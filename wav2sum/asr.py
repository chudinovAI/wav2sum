import copy
import logging
import threading
import warnings

import numpy as np
import torch
from transformers import AutoModel

from wav2sum.audio import SAMPLE_RATE

logger = logging.getLogger(__name__)

MODEL_ID = "ai-sage/GigaAM-v3"
REVISION = "e2e_rnnt"

MAX_SPAN_SEC = 20.0

MAX_BATCH_SECONDS = 160.0
MAX_BATCH_ITEMS = 16


class Transcriber:
    def __init__(self, device: str, lock: "threading.Lock | None" = None):
        self.device = device
        self.lock = lock or threading.Lock()
        logger.info("Loading GigaAM on %s …", device)
        self.model = AutoModel.from_pretrained(MODEL_ID, revision=REVISION, trust_remote_code=True).model
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
