import threading
import time
from typing import ClassVar

import numpy as np
import pytest
import torch

from wav2sum import models
from wav2sum.language import EN, RU, LanguageDetector
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
    for name in ("Transcriber", "EnglishTranscriber", "LanguageDetector", "VoiceActivityDetector", "Diarizer"):
        monkeypatch.setattr(models, name, type(name, (FakeModel,), {}))
    FakeModel.made = []

    loaded = Models("cpu")
    assert FakeModel.made == []
    assert loaded.asr_for(RU) is loaded.asr_for(RU) is loaded.asr
    assert loaded.asr_for(EN) is loaded.english_asr
    assert FakeModel.made == ["Transcriber", "EnglishTranscriber"]

    loaded.preload([RU, EN])
    assert FakeModel.made == ["Transcriber", "EnglishTranscriber", "LanguageDetector", "VoiceActivityDetector"]
    assert loaded.lid.seen == loaded.vad.seen == [16_000]


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
    for name in ("Transcriber", "EnglishTranscriber", "LanguageDetector", "VoiceActivityDetector"):
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
        assert _finishes(lambda: loaded.preload([RU, EN]))
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
        assert _finishes(lambda: (loaded.vad, loaded.lid, loaded.asr, loaded.english_asr))
    finally:
        release.set()
        load.join()


def test_a_model_asked_for_from_two_threads_loads_once(monkeypatch):
    class Transcriber(LockedModel):
        def __init__(self, *args):
            time.sleep(0.1)
            super().__init__(*args)

    loaded = _fake_models(monkeypatch, LockedModel)
    monkeypatch.setattr(models, "Transcriber", Transcriber)
    FakeModel.made = []
    got = []
    threads = [threading.Thread(target=lambda: got.append(loaded.asr)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got[0] is got[1]
    assert FakeModel.made == ["Transcriber"]


def test_explicit_device_wins():
    assert detect_device("cpu") == "cpu"
    assert detect_device() in ("cuda", "mps", "cpu")


class FakeClassifier:
    def __init__(self, ru_score, en_score):
        self.scores = (ru_score, en_score)
        self.batches = []

    def classify_batch(self, wavs, lengths):
        self.batches.append((wavs.shape, lengths.tolist()))
        log_probs = torch.full((len(wavs), 4), -10.0)
        log_probs[:, 1], log_probs[:, 3] = self.scores
        return (log_probs,)


def _detector(ru_score, en_score):
    detector = LanguageDetector.__new__(LanguageDetector)
    detector.model, detector.lock, detector.indices = FakeClassifier(ru_score, en_score), threading.Lock(), [1, 3]
    return detector


def test_language_is_scored_on_the_longest_spans():
    detector = _detector(-0.5, -2.0)
    audio = np.zeros(16_000 * 60, np.float32)
    spans = [(float(i), i + 0.5) for i in range(10)] + [(20.0, 45.0)]
    assert detector.detect(audio, spans) == RU
    [(shape, lengths)] = detector.model.batches
    assert shape == (8, 160_000)
    assert lengths == pytest.approx([1.0] + [0.05] * 7)

    assert _detector(-3.0, -0.1).detect(audio) == EN


def test_no_speech_defaults_to_russian():
    detector = _detector(-3.0, -0.1)
    assert detector.detect(np.zeros(0, np.float32), [(1.0, 2.0)]) == RU
    assert detector.model.batches == []
