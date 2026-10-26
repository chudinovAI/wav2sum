import copy
import logging
import threading
import warnings

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from transformers import AutoConfig, AutoModel

from wav2sum.audio import SAMPLE_RATE

logger = logging.getLogger(__name__)

MODEL_ID = "ai-sage/GigaAM-v3"
REVISION = "e2e_rnnt"
ENGLISH_MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v2"

MAX_SPAN_SEC = 20.0

MAX_BATCH_SECONDS = 160.0
MAX_BATCH_ITEMS = 16


class Transcriber:
    def __init__(self, device: str, lock: "threading.Lock | None" = None):
        self.device = device
        self.lock = lock or threading.Lock()
        logger.info("Loading GigaAM on %s …", device)
        # transformers 5 builds a from_pretrained model on the meta device, and GigaAM's remote code makes real tensors
        # in __init__; building from the config and loading the weights ourselves keeps it on the CPU.
        config = AutoConfig.from_pretrained(MODEL_ID, revision=REVISION, trust_remote_code=True)
        model = AutoModel.from_config(config, trust_remote_code=True)
        weights = hf_hub_download(MODEL_ID, "pytorch_model.bin", revision=REVISION)
        model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
        self.model = model.model
        if device != "cpu":
            # GigaAM already runs the encoder under fp16 autocast off the CPU; fp16 weights just save ~0.6 GB.
            self.model.encoder.half()
        self.model.to(device).eval()
        self.cpu_head = copy.deepcopy(self.model.head).cpu().float()

    def transcribe(self, audio: np.ndarray) -> str:
        return self.transcribe_spans(audio, [(0.0, len(audio) / SAMPLE_RATE)])[0]

    def transcribe_spans(self, audio: np.ndarray, spans: list[tuple[float, float]]) -> list[str]:
        texts = [""] * len(spans)
        for batch in _batches(spans):
            clips = [audio[int(spans[i][0] * SAMPLE_RATE) : int(spans[i][1] * SAMPLE_RATE)] for i in batch]
            with self.lock:
                for i, text in zip(batch, self._recognize(clips), strict=True):
                    texts[i] = text.strip()
        return texts

    @torch.inference_mode()
    def _recognize(self, clips: list[np.ndarray]) -> list[str]:
        lengths = torch.tensor([len(c) for c in clips])
        wav = torch.zeros(len(clips), int(lengths.max()))
        for row, clip in enumerate(clips):
            wav[row, : len(clip)] = torch.from_numpy(clip)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r".*resized since it had shape.*")
            encoded, enc_len = self.model.forward(wav.to(self.device), lengths.to(self.device))
        return self.model.decoding.decode(self.cpu_head, encoded.float().cpu(), enc_len.cpu())


class EnglishTranscriber:
    """Parakeet TDT on MLX; same interface as `Transcriber`."""

    def __init__(self, lock: "threading.Lock | None" = None):
        import mlx.core as mx
        from parakeet_mlx import from_pretrained

        self.lock = lock or threading.Lock()
        logger.info("Loading Parakeet …")
        self.model = from_pretrained(ENGLISH_MODEL_ID)
        # Weights load lazily on this thread's stream, and MLX streams are per thread: a weight first touched
        # from another thread (the decoder's embedding waits for the first non-blank token) fails to evaluate.
        mx.eval(self.model.parameters())

    def transcribe(self, audio: np.ndarray) -> str:
        return self.transcribe_spans(audio, [(0.0, len(audio) / SAMPLE_RATE)])[0]

    def transcribe_spans(self, audio: np.ndarray, spans: list[tuple[float, float]]) -> list[str]:
        # One span at a time: the encoder takes no lengths, so padding a batch would leak into the features.
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        config = self.model.preprocessor_config
        texts = []
        for start, end in spans:
            clip = audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
            if len(clip) < config.hop_length:
                texts.append("")
                continue
            with self.lock:
                mel = get_logmel(mx.array(clip, dtype=mx.float32), config)
                texts.append(self.model.generate(mel)[0].text.strip())
        return texts


def _batches(spans: list[tuple[float, float]]):
    order = sorted(range(len(spans)), key=lambda i: spans[i][0] - spans[i][1])
    batch: list[int] = []
    seconds = 0.0
    for i in order:
        duration = spans[i][1] - spans[i][0]
        if batch and (seconds + duration > MAX_BATCH_SECONDS or len(batch) >= MAX_BATCH_ITEMS):
            yield batch
            batch, seconds = [], 0.0
        batch.append(i)
        seconds += duration
    if batch:
        yield batch
