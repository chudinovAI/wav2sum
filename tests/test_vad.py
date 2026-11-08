import itertools

import numpy as np
import pytest
import torch

from wav2sum import vad


class FakeSilero:
    """The first sample is the probability; retain the exact windows scored."""

    def __init__(self):
        self.windows = []
        self.calls = 0

    def reset_states(self):
        self.calls = 0

    def __call__(self, frame, sample_rate):
        assert sample_rate == 16_000
        assert frame.shape == (512,)
        self.windows.append(frame.numpy().copy())
        self.calls += 1
        return torch.tensor(float(frame[0]))


@pytest.fixture
def detector(monkeypatch):
    monkeypatch.setattr(vad, "load_silero_vad", FakeSilero)
    return vad.VoiceActivityDetector()


def frames(*parts):
    return np.concatenate([np.full(count * 512, probability, dtype=np.float32) for probability, count in parts])


def test_arbitrary_input_splits_score_each_sample_once_and_flush_partial(detector):
    audio = np.concatenate([frames((0.9, 9), (0.0, 40), (0.9, 8)), np.full(137, 0.9, dtype=np.float32)])
    whole = detector.stream()
    whole.add(audio)
    expected = whole.finish()
    split = detector.stream()
    offset = 0
    for size in (1, 510, 3, 900, 17, 819, 2, 8011, 1000, 165, 99):
        split.add(audio[offset : offset + size])
        offset += size
    split.add(audio[offset:])
    assert split.finish() == expected
    scored = np.concatenate(split._model.windows)
    assert np.array_equal(scored[: len(audio)], audio)
    assert not np.any(scored[len(audio) :])
    assert len(scored) == ((len(audio) + 511) // 512) * 512
    assert expected[-1][1] == len(audio) / 16_000
    assert split.finish() == expected


def test_silence_commits_without_another_utterance_and_preview_stops_growing(detector):
    stream = detector.stream()
    stream.add(frames((0.0, 5), (0.9, 12), (0.0, 20)))
    end = stream.spans[-1][1]
    stream.add(frames((0.0, 40)))
    assert stream.completed == stream.spans
    assert len(stream.completed) == 1
    assert stream.completed[0][1] == end == (17 * 512 + 2400) / 16_000
    saved = stream.completed
    saved.clear()
    assert len(stream.completed) == 1


def test_partial_silence_does_not_extend_the_preview(detector):
    stream = detector.stream()
    stream.add(frames((0.9, 10)))
    stream.add(frames((0.0, 1)))
    end = stream.spans[-1][1]
    stream.add(frames((0.0, 8)))
    assert stream.spans[-1][1] == end == 10 * 512 / 16_000


def test_final_partial_can_make_speech_long_enough(detector):
    stream = detector.stream()
    stream.add(np.concatenate([frames((0.9, 6)), np.full(129, 0.9, dtype=np.float32)]))
    assert stream.completed == []
    assert stream.finish() == [(0.0, (6 * 512 + 129) / 16_000)]
    with pytest.raises(ValueError):
        stream.add(np.zeros(512, dtype=np.float32))


def test_short_trigger_after_pending_speech_does_not_change_completed_span(detector):
    stream = detector.stream()
    stream.add(frames((0.9, 10), (0.0, 17), (0.9, 3), (0.0, 40)))
    assert stream.completed == [(0.0, (10 * 512 + 2400) / 16_000)]
    assert stream.finish() == stream.completed


def test_short_trigger_dropped_and_nearby_real_speech_merges(detector):
    stream = detector.stream()
    stream.add(frames((0.9, 3), (0.0, 40)))
    assert stream.completed == []
    stream.add(frames((0.9, 10), (0.0, 24), (0.9, 10), (0.0, 40)))
    spans = stream.finish()
    assert len(spans) == 1
    assert spans[0][0] == (43 * 512 - 2400) / 16_000
    assert spans[0][1] == (87 * 512 + 2400) / 16_000


def test_separated_spans_have_padding_without_overlap(detector):
    stream = detector.stream()
    stream.add(frames((0.0, 10), (0.9, 10), (0.0, 35), (0.9, 10), (0.0, 40)))
    first, second = stream.finish()
    assert first == ((10 * 512 - 2400) / 16_000, (20 * 512 + 2400) / 16_000)
    assert second == ((55 * 512 - 2400) / 16_000, (65 * 512 + 2400) / 16_000)
    assert first[1] < second[0]


def test_continuous_speech_is_split_at_max_and_has_no_overlap(detector):
    stream = detector.stream()
    stream.add(frames((0.9, 1300)))
    spans = stream.finish()
    assert len(spans) == 3
    assert spans[0][0] == 0
    assert spans[-1][1] == 1300 * 512 / 16_000
    assert all(0 < end - start <= 20 for start, end in spans)
    assert all(left[1] == right[0] for left, right in itertools.pairwise(spans))


def test_silence_at_exact_max_does_not_create_padding_only_span(detector):
    stream = detector.stream()
    stream.add(frames((0.9, 625), (0.0, 40)))
    assert stream.completed == [(0.0, 20.0)]
    assert stream.finish() == [(0.0, 20.0)]


def test_clones_keep_recurrent_state_isolated_from_streams_and_batch(monkeypatch):
    class StatefulSilero(FakeSilero):
        def __call__(self, frame, sample_rate):
            super().__call__(frame, sample_rate)
            return torch.tensor(0.9 if self.calls <= 10 else 0.0)

    monkeypatch.setattr(vad, "load_silero_vad", StatefulSilero)

    def batch(_audio, model, **_kwargs):
        model.reset_states()
        model(torch.zeros(512), 16_000)
        return []

    monkeypatch.setattr(vad, "get_speech_timestamps", batch)
    detector = vad.VoiceActivityDetector()
    first, second = detector.stream(), detector.stream()
    reference = detector.stream()
    audio = frames((0.0, 50))
    reference.add(audio)
    first.add(audio[: 7 * 512])
    detector.speech_spans(audio)
    second.add(audio[: 20 * 512])
    first.add(audio[7 * 512 :])
    second.add(audio[20 * 512 :])
    assert first.finish() == second.finish() == reference.finish()
    assert first._model is not second._model
    assert first._model.calls == second._model.calls == reference._model.calls == 50


def test_stream_model_is_reset_even_if_batch_already_used(detector):
    detector.model.calls = 42
    detector.model.windows = [np.ones(512)]
    stream = detector.stream()
    assert stream._model.calls == 0
    assert detector.model.calls == 42
