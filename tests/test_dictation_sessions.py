import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from wav2sum import vad
from wav2sum.config import AUTO, DictationConfig
from wav2sum.dictation import DictationStream, Dictator
from wav2sum.vocabulary import Vocabulary


class ProbabilityModel:
    def reset_states(self):
        pass

    def __call__(self, audio, sampling_rate):
        return torch.tensor(float(bool(torch.count_nonzero(audio))))


class PhraseRecognizer:
    def __init__(self):
        self.windows = []
        self.contexts = []

    def transcribe_spans(self, audio, spans, language=None, *, terms=()):
        self.windows.append(len(audio) / 16000)
        self.contexts.append(terms)
        result = []
        for start, end in spans:
            clip = audio[round(start * 16000) : round(end * 16000)]
            words = []
            if np.any((clip > 0) & (clip < 0.5)):
                words.append("hello")
            if np.any(clip >= 0.5):
                words.append("мир")
            result.append(" ".join(words))
        return result


@pytest.fixture
def dictator(monkeypatch):
    monkeypatch.setattr(vad, "load_silero_vad", ProbabilityModel)
    models = SimpleNamespace(cloud=False, vad=vad.VoiceActivityDetector(), dictation_asr=PhraseRecognizer())
    return Dictator(models, DictationConfig(cleanup=False), language=AUTO)


def speech(seconds, value=0.2):
    return np.full(round(seconds * 16000), value, dtype=np.float32)


def test_silence_commits_without_next_utterance_and_preview_does_not_become_final(dictator):
    stream = DictationStream(dictator)
    stream.add(speech(1))
    assert stream.step(preview=True) == "hello"
    assert not stream.texts
    stream.add(speech(1, 0))
    assert stream.step(preview=True) == "hello"
    assert list(stream.texts.values()) == ["hello"]
    stream.add(speech(1, 0.8))
    assert stream.step(preview=True) == "hello мир"
    assert stream.finish() == ("hello мир", "en")


def test_long_local_session_keeps_recognition_audio_bounded(dictator):
    stream = DictationStream(dictator)
    for _ in range(70):
        stream.add(speech(1))
        stream.step()
    text, language = stream.finish()
    assert text.split() == ["hello"] * 4
    assert language == "en"
    assert max(dictator.models.dictation_asr.windows) <= 22
    assert stream.samples == 70 * 16000


def test_session_context_is_fixed_until_next_dictation(dictator):
    dictator.cfg.dictionary = ["Qwen"]
    stream = DictationStream(dictator, app="com.apple.mail")
    dictator.cfg.dictionary.append("Nemotron")
    dictator.vocabulary.pairs["гитхаб"] = "GitHub"
    stream.add(speech(1))
    stream.step(preview=True)
    assert dictator.models.dictation_asr.contexts == [("Qwen",)]
    assert stream.context.style == "email"
    assert stream.context.fixes == ()
    next_stream = DictationStream(dictator)
    assert next_stream.context.terms == ("Qwen", "Nemotron", "GitHub")


def test_cancelled_stream_rejects_tail_and_cannot_return_cached_preview(dictator):
    stream = DictationStream(dictator)
    stream.add(speech(1))
    assert stream.step(preview=True) == "hello"
    stream.close()
    with pytest.raises(RuntimeError, match="cancel"):
        stream.finish()
    with pytest.raises(RuntimeError, match="cancel"):
        stream.add(speech(1))


def test_chunk_boundaries_do_not_change_final_transcript(dictator):
    audio = np.concatenate([speech(1), speech(0.8, 0), speech(1, 0.8), speech(0.8, 0)])
    results = []
    for size in (127, 4000, 16000, len(audio)):
        stream = DictationStream(dictator)
        for start in range(0, len(audio), size):
            stream.add(audio[start : start + size])
            if size >= 4000:
                stream.step()
        results.append(stream.finish())
    assert results == [("hello мир", "en")] * 4


def test_empty_or_cancelled_cloud_session_does_not_open_a_provider():
    class CloudModels:
        cloud = True

        @property
        def dictation_asr(self):
            pytest.fail("an empty session must not open a billable connection")

    dictator = Dictator(CloudModels(), DictationConfig(), vocabulary=Vocabulary(), language=AUTO)
    stream = DictationStream(dictator)
    stream.add(np.empty(0, np.float32))
    assert stream.step(preview=True) is None
    assert stream.finish() == ("", "ru")
    assert stream.sent_seconds == 0
    stream.close()

    cancelled = DictationStream(dictator)
    cancelled.close()
    with pytest.raises(RuntimeError, match="cancel"):
        cancelled.add(speech(1))
    with pytest.raises(RuntimeError, match="cancel"):
        cancelled.finish()


class BatchRecognizer(PhraseRecognizer):
    """Controllable upload that recognizes speech only when finalized."""

    def __init__(self):
        super().__init__()
        self.uploads = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.error = None

    def start_dictation(self, *, language=None, terms=(), timeout=20):
        upload = FakeUpload(self, language, terms, timeout)
        self.uploads.append(upload)
        return upload


class FakeUpload:
    def __init__(self, recognizer, language, terms, timeout):
        self.recognizer = recognizer
        self.language = language
        self.terms = terms
        self.timeout = timeout
        self.chunks = []
        self.sent_samples = 0
        self.finishes = 0
        self.closed = False

    def add(self, audio):
        self.chunks.append(audio.copy())
        self.sent_samples += len(audio)

    def finish(self):
        self.finishes += 1
        self.recognizer.entered.set()
        assert self.recognizer.release.wait(3)
        if self.recognizer.error:
            raise self.recognizer.error
        audio = np.concatenate(self.chunks)
        return self.recognizer.transcribe_spans(audio, [(0, len(audio) / 16_000)], self.language, terms=self.terms)[0]

    def close(self):
        self.closed = True


@pytest.fixture
def cloud_dictator():
    models = SimpleNamespace(cloud=True, dictation_asr=BatchRecognizer())
    return Dictator(models, DictationConfig(cleanup=False), vocabulary=Vocabulary(), language=AUTO)


@pytest.mark.parametrize("size", [127, 4000, 20_000])
def test_cloud_upload_starts_during_speech_and_finalizes_owned_tail(cloud_dictator, size):
    cloud_dictator.cfg.dictionary = ["Qwen"]
    stream = DictationStream(cloud_dictator)
    cloud_dictator.cfg.dictionary.append("Nemotron")
    cloud_dictator.language = "ru"
    audio = np.concatenate([speech(1), speech(0.25, 0.8)])
    asr = cloud_dictator.models.dictation_asr
    stream.add(np.empty(0, np.float32))
    assert asr.uploads == []
    for start in range(0, len(audio), size):
        chunk = audio[start : start + size].copy()
        stream.add(chunk)
        chunk.fill(-1)
        assert len(asr.uploads) == 1
        assert stream.step(preview=True) is None
    assert stream.preview_seconds is None
    assert stream.sent_seconds == 1.25
    assert stream.finish() == ("hello мир", "en")
    assert stream.finish() == ("hello мир", "en")
    upload = asr.uploads[0]
    assert upload.finishes == 1
    assert (upload.language, upload.terms, upload.timeout) == (None, ("Qwen",), 20)
    np.testing.assert_array_equal(np.concatenate(upload.chunks), audio)
    assert asr.contexts == [("Qwen",)]
    with pytest.raises(RuntimeError):
        stream.add(speech(0.25))


def test_cloud_final_applies_frozen_terms_and_explicit_language(cloud_dictator):
    cloud_dictator.cfg.dictionary = ["Nemotron"]
    cloud_dictator.language = "ru"
    stream = DictationStream(cloud_dictator)
    cloud_dictator.cfg.dictionary.clear()
    cloud_dictator.language = "en"
    asr = cloud_dictator.models.dictation_asr
    asr.transcribe_spans = lambda audio, spans, language, *, terms: ["Немotron"]
    stream.add(speech(1))
    assert stream.finish() == ("Nemotron", "ru")
    assert asr.uploads[0].language == "ru"
    assert asr.uploads[0].terms == ("Nemotron",)


def test_cloud_cancel_after_audio_closes_upload_and_rejects_result(cloud_dictator):
    stream = DictationStream(cloud_dictator)
    stream.add(speech(1))
    upload = cloud_dictator.models.dictation_asr.uploads[0]
    stream.close()
    assert upload.closed
    with pytest.raises(RuntimeError, match="cancel"):
        stream.finish()
    with pytest.raises(RuntimeError, match="cancel"):
        stream.add(speech(1))
    assert upload.finishes == 0


def test_failed_cloud_upload_finish_is_not_replayed(cloud_dictator):
    asr = cloud_dictator.models.dictation_asr
    asr.error = RuntimeError("provider unavailable")
    stream = DictationStream(cloud_dictator)
    stream.add(speech(1))
    with pytest.raises(RuntimeError):
        stream.finish()
    with pytest.raises(RuntimeError):
        stream.finish()
    assert len(asr.uploads) == 1
    assert asr.uploads[0].finishes == 1


def test_cloud_upload_concurrent_finish_is_cached(cloud_dictator):
    asr = cloud_dictator.models.dictation_asr
    asr.release.clear()
    second_started = threading.Event()
    stream = DictationStream(cloud_dictator)
    stream.add(speech(1))

    def second():
        second_started.set()
        return stream.finish()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(stream.finish)
        try:
            assert asr.entered.wait(3)
            next_result = pool.submit(second)
            assert second_started.wait(3)
        finally:
            asr.release.set()
        assert first.result(timeout=3) == next_result.result(timeout=3) == ("hello", "en")
    assert len(asr.uploads) == 1
    assert asr.uploads[0].finishes == 1


def test_cloud_upload_cancel_during_finish_discards_late_result_without_blocking(cloud_dictator):
    asr = cloud_dictator.models.dictation_asr
    asr.release.clear()
    stream = DictationStream(cloud_dictator)
    stream.add(speech(1))
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(stream.finish)
        try:
            assert asr.entered.wait(3)
            with pytest.raises(RuntimeError):
                stream.add(speech(0.25))
            pool.submit(stream.close).result(timeout=1)
            assert asr.uploads[0].closed
        finally:
            asr.release.set()
        with pytest.raises(RuntimeError, match="cancel"):
            result.result(timeout=3)
    with pytest.raises(RuntimeError, match="cancel"):
        stream.finish()
    assert len(asr.uploads) == 1


def test_cloud_direct_recognize_accepts_one_large_chunk(cloud_dictator):
    assert cloud_dictator.recognize(np.concatenate([speech(80), speech(0.25, 0.8)])) == ("hello мир", "en")
    assert cloud_dictator.models.dictation_asr.uploads[0].sent_samples == round(80.25 * 16_000)
