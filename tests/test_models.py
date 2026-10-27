import threading
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
