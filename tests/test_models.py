import threading
import time
from typing import ClassVar

import pytest
import torch

from wav2sum import models
from wav2sum.language import RU
from wav2sum.models import Models, detect_device


class FakeModel:
    made: ClassVar[list[str]] = []

    def __init__(self, *args):
        FakeModel.made.append(type(self).__name__)
        self.seen = []

    def transcribe(self, audio):
        self.seen.append(len(audio))
        return ""

    def detect(self, audio, spans=None):
        self.seen.append(len(audio))
        return RU

    def speech_spans(self, audio):
        self.seen.append(len(audio))
        return []


def test_models_load_lazily_once_and_preload_what_dictation_needs(monkeypatch):
    for name in (
        "QwenTranscriber",
        "VoiceActivityDetector",
        "Diarizer",
    ):
        monkeypatch.setattr(models, name, type(name, (FakeModel,), {}))
    FakeModel.made = []

    loaded = Models("cpu")
    assert FakeModel.made == []
    loaded.preload_dictation()
    assert FakeModel.made == ["QwenTranscriber", "VoiceActivityDetector"]
    assert loaded.dictation_asr is loaded.dictation_asr
    assert loaded.dictation_asr.seen == loaded.vad.seen == [16_000]
    assert FakeModel.made == ["QwenTranscriber", "VoiceActivityDetector"]


class LockedModel(FakeModel):
    """Takes a lock the way the real models do: the one it is given, or its own."""

    def __init__(self, *args):
        super().__init__(*args)
        self.lock = next((a for a in args if hasattr(a, "acquire")), None) or threading.Lock()

    def transcribe(self, audio):
        with self.lock:
            return super().transcribe(audio)

    def detect(self, audio, spans=None):
        with self.lock:
            return super().detect(audio, spans)

    def speech_spans(self, audio):
        with self.lock:
            return super().speech_spans(audio)


def _fake_models(monkeypatch, diarizer):
    for name in ("QwenTranscriber", "VoiceActivityDetector"):
        monkeypatch.setattr(models, name, type(name, (LockedModel,), {}))
    monkeypatch.setattr(models, "Diarizer", diarizer)
    return Models("cpu")


def _finishes(call) -> bool:
    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(1)
    return not worker.is_alive()


def test_dictation_does_not_wait_for_a_running_diarization(monkeypatch):
    started, release = threading.Event(), threading.Event()

    class Diarizer(LockedModel):
        def diarize(self, audio):
            with self.lock:
                started.set()
                release.wait(5)

    loaded = _fake_models(monkeypatch, Diarizer)
    call = threading.Thread(target=loaded.diarizer.diarize, args=(None,))
    call.start()
    started.wait(5)
    try:
        assert _finishes(loaded.preload_dictation)
    finally:
        release.set()
        call.join()


def test_dictation_does_not_wait_for_the_diarizer_to_load(monkeypatch):
    started, release = threading.Event(), threading.Event()

    class Diarizer(LockedModel):
        def __init__(self, *args):
            started.set()
            release.wait(5)
            super().__init__(*args)

    loaded = _fake_models(monkeypatch, Diarizer)
    load = threading.Thread(target=lambda: loaded.diarizer)
    load.start()
    started.wait(5)
    try:
        assert _finishes(lambda: (loaded.vad, loaded.dictation_asr))
    finally:
        release.set()
        load.join()


def test_a_model_asked_for_from_two_threads_loads_once(monkeypatch):
    class QwenTranscriber(LockedModel):
        def __init__(self, *args):
            time.sleep(0.1)
            super().__init__(*args)

    loaded = _fake_models(monkeypatch, LockedModel)
    monkeypatch.setattr(models, "QwenTranscriber", QwenTranscriber)
    FakeModel.made = []
    got = []
    threads = [threading.Thread(target=lambda: got.append(loaded.dictation_asr)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got[0] is got[1]
    assert FakeModel.made == ["QwenTranscriber"]


def test_explicit_device_wins():
    assert detect_device("cpu") == "cpu"
    assert detect_device() in ("cuda", "mps", "cpu")


def test_unload_frees_a_model_and_the_mps_cache(monkeypatch):
    monkeypatch.setattr(models, "Diarizer", type("Diarizer", (FakeModel,), {}))
    emptied = []
    monkeypatch.setattr(torch.mps, "empty_cache", lambda: emptied.append(True))

    loaded = Models("mps")
    loaded.unload("diarizer")
    first = loaded.diarizer
    loaded.unload("diarizer")
    assert loaded.diarizer is not first
    assert emptied == [True, True]

    Models("cpu").unload("diarizer")
    assert emptied == [True, True]


def test_call_session_closes_on_success_and_error(monkeypatch):
    made = []

    class Worker:
        def __init__(self, device):
            self.closed = False
            made.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(models, "NemotronTranscriber", Worker)
    registry = Models("cpu", allow_unsafe_nemotron=True)
    with registry.call_session():
        assert registry.call_asr is registry.call_asr
    assert made[0].closed
    with pytest.raises(RuntimeError, match="broken"), registry.call_session():
        assert registry.call_asr is not made[0]
        raise RuntimeError("broken")
    assert made[1].closed


def test_qwen_call_backend_is_scoped_and_distinct(monkeypatch):
    monkeypatch.setattr(models, "QwenTranscriber", type("QwenTranscriber", (FakeModel,), {}))
    registry = Models("cpu", call_model="qwen")
    with registry.call_session():
        assert registry.call_asr is registry.call_asr
    assert "call_asr" not in registry._loaded


def test_cloud_preload_validates_without_transcribing(monkeypatch):
    calls = []

    class CloudASR:
        def validate_credentials(self):
            calls.append("credentials")

    monkeypatch.setattr(models, "DeepgramTranscriber", CloudASR)
    monkeypatch.setattr(models, "QwenTranscriber", lambda *_: pytest.fail("local ASR loaded"))
    registry = Models("cpu", cloud=True)
    registry.preload_dictation()
    assert calls == ["credentials"]


def test_default_local_never_constructs_cloud_provider(monkeypatch):
    monkeypatch.setattr(models, "DeepgramTranscriber", lambda: pytest.fail("cloud provider constructed"))
    monkeypatch.setattr(models, "QwenTranscriber", type("QwenTranscriber", (FakeModel,), {}))
    monkeypatch.setattr(models, "VoiceActivityDetector", type("VoiceActivityDetector", (FakeModel,), {}))
    registry = Models("cpu")
    registry.preload_dictation()
    assert not registry.cloud
