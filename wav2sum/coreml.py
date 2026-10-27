"""Convert the GigaAM-v3 encoder to Core ML and compare it with torch fp32 (CHU-98).

uv run --group coreml python -m wav2sum.coreml
"""

import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import torch

from wav2sum.asr import Transcriber
from wav2sum.audio import SAMPLE_RATE, load_audio
from wav2sum.config import STATE_DIR, load_config

OUT = STATE_DIR / "gigaam-encoder.mlpackage"
SHAPE_SECONDS = (5, 10, 20)
CLIP_SECONDS = (3, 8, 17)
RECORDINGS = 3


class MaskedEncoder(torch.nn.Module):
    """GigaAM's encoder forward, but with the attention mask it only builds for batches > 1:
    a clip padded to a fixed shape must not attend to its padding."""

    def __init__(self, encoder: torch.nn.Module):
        super().__init__()
        self.encoder = encoder
        for layer in encoder.layers:
            # matmul + softmax instead of SDPA: on the Neural Engine SDPA lets padding leak through its bool mask.
            layer.self_attn.torch_sdpa_attn = False
        encoder.pos_enc.extend_pe(encoder.pos_emb_max_len, torch.device("cpu"))  # GigaAM builds it in forward
        # GigaAM's pos_enc adds `pe.shape[0] // 2 + frames` as a tensor, and fp16 rounds it above 4096.
        self.half = encoder.pos_enc.pe.size(0) // 2

    def forward(self, features: torch.Tensor, length: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        x, length = self.encoder.pre_encode(x=features.transpose(1, 2), lengths=length)
        pe = self.encoder.pos_enc.pe
        pos_emb = [pe[: x.size(1)], pe[self.half : self.half + x.size(1)]]
        valid = torch.arange(x.size(1), device=x.device).unsqueeze(0) < length.unsqueeze(-1)
        att_mask = ~(valid.unsqueeze(1) & valid.unsqueeze(2))
        for layer in self.encoder.layers:
            x = layer(x=x, pos_emb=pos_emb, att_mask=att_mask, pad_mask=~valid)
        return x.transpose(1, 2), length


def pad(features: torch.Tensor, frames: list[int]) -> torch.Tensor:
    fits = [n for n in frames if n >= features.size(-1)]
    if not fits:
        raise ValueError(f"{features.size(-1)} feature frames is longer than {max(frames)} frames")
    return torch.nn.functional.pad(features, (0, min(fits) - features.size(-1)))


def main():
    import coremltools as ct
    from coremltools.models.utils import MultiFunctionDescriptor, save_multifunction

    _register_int_op()
    model = Transcriber("cpu").model
    encoder = MaskedEncoder(model.encoder).eval()
    shapes = {s: _features(model, np.zeros(s * SAMPLE_RATE, dtype=np.float32)).shape for s in SHAPE_SECONDS}
    frames = {shape[-1]: s for s, shape in shapes.items()}

    # Enumerated shapes put the whole model on the CPU, so one fixed shape per function, sharing the weights.
    descriptor = MultiFunctionDescriptor()
    with tempfile.TemporaryDirectory() as tmp:
        for seconds, shape in shapes.items():
            traced = torch.jit.trace(encoder, (torch.zeros(shape), torch.tensor([shape[-1]])))
            mlmodel = ct.convert(
                traced,
                inputs=[
                    ct.TensorType(name="features", shape=shape),
                    ct.TensorType(name="length", shape=(1,), dtype=np.int32),
                ],
                outputs=[ct.TensorType(name="encoded"), ct.TensorType(name="encoded_length")],
                convert_to="mlprogram",
                compute_precision=ct.precision.FLOAT16,
                compute_units=ct.ComputeUnit.CPU_AND_NE,
                minimum_deployment_target=ct.target.macOS15,
            )
            print(f"{seconds} s:")
            _print_compute_plan(ct, mlmodel)
            path = str(Path(tmp) / f"s{seconds}.mlpackage")
            mlmodel.save(path)
            descriptor.add_function(path, src_function_name="main", target_function_name=f"s{seconds}")
        descriptor.default_function_name = f"s{max(SHAPE_SECONDS)}"
        OUT.parent.mkdir(parents=True, exist_ok=True)
        save_multifunction(descriptor, str(OUT))
    print(f"Saved {OUT}")

    functions = {
        n: ct.models.MLModel(str(OUT), function_name=f"s{s}", compute_units=ct.ComputeUnit.CPU_AND_NE)
        for n, s in frames.items()
    }
    for path in sorted(load_config().recordings_dir.glob("*.wav"))[-RECORDINGS:]:
        audio = load_audio(path, channels=1)[:, 0]
        for seconds in CLIP_SECONDS:
            start = len(audio) // 2
            clip = _features(model, audio[start : start + seconds * SAMPLE_RATE])
            length = torch.tensor([clip.size(-1)])
            with torch.no_grad():
                expected, expected_length = model.encoder(clip, length)
            padded = pad(clip, list(frames))
            out = functions[padded.size(-1)].predict(
                {"features": padded.numpy(), "length": length.numpy().astype(np.int32)}
            )
            encoded = torch.from_numpy(out["encoded"][..., : int(expected_length)]).float()
            cosine = torch.nn.functional.cosine_similarity(encoded.flatten(), expected.flatten(), dim=0)
            print(f"{path.name} {seconds:>2} s: cosine {cosine:.4f}")


def _register_int_op():
    from coremltools.converters.mil import Builder as mb
    from coremltools.converters.mil.frontend.torch import ops
    from coremltools.converters.mil.frontend.torch.torch_op_registry import register_torch_op

    @register_torch_op(torch_alias=["int"], override=True)
    def _int(context, node):
        # coremltools folds GigaAM's `x.shape[-1] // 2` to a (1,) array, and NumPy 2 cannot int() it.
        x = context[node.inputs[0]]
        if x.val is None:
            return ops._cast(context, node, int, "int32")
        context.add(mb.const(val=int(np.asarray(x.val).item()), name=node.name))


def _features(model, audio: np.ndarray) -> torch.Tensor:
    with torch.no_grad():  # not inference_mode: jit.trace refuses inference tensors
        features, _ = model.preprocessor(torch.from_numpy(audio)[None], torch.tensor([len(audio)]))
    return features


def _print_compute_plan(ct, mlmodel):
    from coremltools.models.compute_plan import MLComputePlan

    plan = MLComputePlan.load_from_path(path=mlmodel.get_compiled_model_path(), compute_units=ct.ComputeUnit.CPU_AND_NE)
    devices = Counter()
    off_ane = Counter()
    for op in plan.model_structure.program.functions["main"].block.operations:
        usage = plan.get_compute_device_usage_for_mlprogram_operation(op)
        device = type(usage.preferred_compute_device).__name__ if usage else None
        if device is None and op.operator_name == "const":
            continue
        devices[device] += 1
        if device != "MLNeuralEngineComputeDevice":
            off_ane[(device, op.operator_name)] += 1
    print("  operations by device:", dict(devices))
    for (device, name), count in off_ane.most_common():
        print(f"    {device}: {name} × {count}")


if __name__ == "__main__":
    main()
