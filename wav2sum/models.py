import gc
import threading
from contextlib import contextmanager

import numpy as np
import torch

from wav2sum.asr import QwenTranscriber
from wav2sum.cloud import DeepgramTranscriber
from wav2sum.diarizer import Diarizer
from wav2sum.local_models import check_memory
from wav2sum.nemotron import NemotronTranscriber
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
    def __init__(
        self,
        device: str | None = None,
        *,
        cloud: bool = False,
        call_model: str = "nemotron",
        allow_unsafe_nemotron: bool = False,
    ):
        if call_model not in ("qwen", "nemotron"):
            raise ValueError("Unknown local call model")
        self.device = detect_device(device)
        self.cloud = cloud
        self.call_model = call_model
        self.allow_unsafe_nemotron = allow_unsafe_nemotron
        self._loaded: dict[str, object] = {}
        # Dictation and call models do not block each other.
        self._locks: dict[str, threading.Lock] = {}
        self._call_session_lock = threading.Lock()

    @property
    def dictation_asr(self) -> QwenTranscriber | DeepgramTranscriber:
        return self._load("dictation_asr", DeepgramTranscriber if self.cloud else lambda: QwenTranscriber(self.device))

    @property
    def call_asr(self) -> NemotronTranscriber | QwenTranscriber:
        if self.cloud:
            raise RuntimeError("local call ASR is unavailable in cloud mode")
        if self.call_model == "qwen":
            return self._load("call_asr", lambda: QwenTranscriber(self.device))
        check_memory("nemotron", allow_unsafe=self.allow_unsafe_nemotron)
        return self._load("call_asr", lambda: NemotronTranscriber(self.device))

    @contextmanager
    def call_session(self):
        with self._call_session_lock:
            try:
                yield
            finally:
                self.unload("call_asr")

    @property
    def vad(self) -> VoiceActivityDetector:
        return self._load("vad", VoiceActivityDetector)

    @property
    def diarizer(self) -> Diarizer:
        return self._load("diarizer", lambda: Diarizer(self.device))

    def preload_dictation(self) -> None:
        if self.cloud:
            self.dictation_asr.validate_credentials()
            return
        silence = np.zeros(16_000, dtype=np.float32)
        self.dictation_asr.transcribe(silence)
        self.vad.speech_spans(silence)

    def unload(self, name: str) -> None:
        with self._locks.setdefault(name, threading.Lock()):
            model = self._loaded.pop(name, None)
            if name == "call_asr" and model is not None and hasattr(model, "close"):
                model.close()
            gc.collect()
            if self.device == "mps":
                torch.mps.empty_cache()

    def _load(self, name, factory):
        with self._locks.setdefault(name, threading.Lock()):
            if name not in self._loaded:
                self._loaded[name] = factory()
            return self._loaded[name]
