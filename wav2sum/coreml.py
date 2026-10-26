"""Convert the GigaAM-v3 encoder to Core ML and compare it with torch fp32 (CHU-98).

uv run --group coreml python -m wav2sum.coreml
"""

from collections import Counter

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

    def forward(self, features: torch.Tensor, length: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        x, length = self.encoder.pre_encode(x=features.transpose(1, 2), lengths=length)
        x, pos_emb = self.encoder.pos_enc(x=x)
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

    model = Transcriber("cpu").model
    features = {s: _features(model, np.zeros(s * SAMPLE_RATE, dtype=np.float32)) for s in SHAPE_SECONDS}
    frames = [f.size(-1) for f in features.values()]
    longest = features[max(SHAPE_SECONDS)]

    traced = torch.jit.trace(MaskedEncoder(model.encoder).eval(), (longest, torch.tensor([longest.size(-1)])))
    mlmodel = ct.convert(
        traced,
        inputs=[
            ct.TensorType(name="features", shape=ct.EnumeratedShapes([(*longest.shape[:2], n) for n in frames])),
            ct.TensorType(name="length", shape=(1,), dtype=np.int32),
        ],
        outputs=[ct.TensorType(name="encoded"), ct.TensorType(name="encoded_length")],
        convert_to="mlprogram",
        compute_precision=ct.precision.FLOAT16,
        compute_units=ct.ComputeUnit.CPU_AND_NE,
        minimum_deployment_target=ct.target.macOS15,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    mlmodel.save(str(OUT))
    print(f"Saved {OUT}")

    for path in sorted(load_config().recordings_dir.glob("*.wav"))[-RECORDINGS:]:
        audio = load_audio(path, channels=1)[:, 0]
        for seconds in CLIP_SECONDS:
            start = len(audio) // 2
            clip = _features(model, audio[start : start + seconds * SAMPLE_RATE])
            length = torch.tensor([clip.size(-1)])
            with torch.inference_mode():
                expected, expected_length = model.encoder(clip, length)
            out = mlmodel.predict({"features": pad(clip, frames).numpy(), "length": length.numpy().astype(np.int32)})
            encoded = torch.from_numpy(out["encoded"][..., : int(expected_length)]).float()
            cosine = torch.nn.functional.cosine_similarity(encoded.flatten(), expected.flatten(), dim=0)
            print(f"{path.name} {seconds:>2} s: cosine {cosine:.4f}")

    _print_compute_plan(ct, mlmodel)


def _features(model, audio: np.ndarray) -> torch.Tensor:
    with torch.inference_mode():
        features, _ = model.preprocessor(torch.from_numpy(audio)[None], torch.tensor([len(audio)]))
    return features


def _print_compute_plan(ct, mlmodel):
    from coremltools.models.compute_plan import MLComputePlan

    plan = MLComputePlan.load_from_path(path=mlmodel.get_compiled_model_path(), compute_units=ct.ComputeUnit.CPU_AND_NE)
    devices = Counter()
    off_ane = Counter()
    for op in plan.model_structure.program.functions["main"].block.operations:
        usage = plan.get_compute_device_usage_for_mlprogram_operation(op)
        if usage is None:  # consts and other ops that do not run
            continue
        device = type(usage.preferred_compute_device).__name__
        devices[device] += 1
        if device != "MLNeuralEngineComputeDevice":
            off_ane[(device, op.operator_name)] += 1
    print("Operations by device:", dict(devices))
    for (device, name), count in off_ane.most_common():
        print(f"  {device}: {name} × {count}")


if __name__ == "__main__":
    main()
