import threading

import numpy as np
import torch

from wav2sum.asr import Transcriber
from wav2sum.diarizer import Diarizer
from wav2sum.vad import VoiceActivityDetector


def detect_device(requested: str | None = None) -> str:
    if requested:
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Models:
    def __init__(self, device: str | None = None):
        self.device = detect_device(device)
        self.lock = threading.Lock()
        self._loaded: dict[str, object] = {}

    @property
    def asr(self) -> Transcriber:
        return self._load("asr", lambda: Transcriber(self.device, self.lock))

    @property
    def vad(self) -> VoiceActivityDetector:
        return self._load("vad", lambda: VoiceActivityDetector(self.lock))

    @property
    def diarizer(self) -> Diarizer:
        return self._load("diarizer", lambda: Diarizer(self.device, self.lock))

    def preload(self) -> None:
        silence = np.zeros(16_000, dtype=np.float32)
        self.asr.transcribe(silence)
        self.vad.speech_spans(silence)

    def _load(self, name, factory):
        with self.lock:
            if name not in self._loaded:
                self._loaded[name] = factory()
            return self._loaded[name]
