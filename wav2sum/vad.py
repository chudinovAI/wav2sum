import copy
import threading

import numpy as np
import torch
from silero_vad import get_speech_timestamps, load_silero_vad

from wav2sum.asr import MAX_SPAN_SEC
from wav2sum.audio import SAMPLE_RATE

MERGE_GAP_SEC = 0.6

_WINDOW = 512
_PAD = SAMPLE_RATE * 150 // 1000
_MIN_SPEECH = SAMPLE_RATE * 200 // 1000
_MIN_SILENCE = SAMPLE_RATE * 500 // 1000
_MERGE_GAP = int(SAMPLE_RATE * MERGE_GAP_SEC)
_MAX_SPAN = int(SAMPLE_RATE * MAX_SPAN_SEC)


class VoiceActivityDetector:
    def __init__(self, lock: "threading.Lock | None" = None):
        self.lock = lock or threading.Lock()
        self.model = load_silero_vad()

    def stream(self) -> "VoiceActivityStream":
        # Silero's recurrent state is mutable. Clone once under the same lock
        # that protects batch inference, then never share the clone.
        with self.lock:
            model = copy.deepcopy(self.model)
        model.reset_states()
        return VoiceActivityStream(model)

    def speech_spans(self, audio: np.ndarray, merge_gap_sec: float = MERGE_GAP_SEC) -> list[tuple[float, float]]:
        with self.lock:
            stamps = get_speech_timestamps(
                torch.from_numpy(np.ascontiguousarray(audio)),
                self.model,
                sampling_rate=SAMPLE_RATE,
                min_speech_duration_ms=200,
                min_silence_duration_ms=500,
                max_speech_duration_s=MAX_SPAN_SEC,
                speech_pad_ms=150,
            )
        spans = [(s["start"] / SAMPLE_RATE, s["end"] / SAMPLE_RATE) for s in stamps]
        return merge_close_spans(spans, merge_gap_sec, MAX_SPAN_SEC)


class VoiceActivityStream:
    """Incremental Silero segmentation. Calls on one stream must be serialized."""

    def __init__(self, model):
        self._model = model
        self._buffer = np.empty(_WINDOW, dtype=np.float32)
        self._buffered = 0
        self._processed = 0
        self._finished = False
        self._completed: list[tuple[int, int]] = []
        self._pending: tuple[int, int] | None = None
        self._start: int | None = None
        self._raw_start = 0
        self._speech_end = 0
        self._silence_start: int | None = None
        self._qualified = False

    @staticmethod
    def _seconds(spans: list[tuple[int, int]]) -> list[tuple[float, float]]:
        return [(start / SAMPLE_RATE, end / SAMPLE_RATE) for start, end in spans]

    @property
    def completed(self) -> list[tuple[float, float]]:
        return self._seconds(self._completed)

    @property
    def spans(self) -> list[tuple[float, float]]:
        spans = self._completed.copy()
        if self._pending is not None:
            spans.append(self._pending)
        if self._start is not None:
            end = self._active_end(preview=True)
            start = self._start
            if spans and start - spans[-1][1] < _MERGE_GAP and end - spans[-1][0] <= _MAX_SPAN:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                if spans:
                    start = max(start, spans[-1][1])
                if end > start:
                    spans.append((start, end))
        return self._seconds(spans)

    def add(self, audio: np.ndarray) -> None:
        if self._finished:
            raise ValueError("Cannot add audio after finishing a VAD stream")
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if audio.ndim != 1:
            raise ValueError("VAD stream audio must be one-dimensional")
        offset = 0
        if self._buffered:
            count = min(_WINDOW - self._buffered, len(audio))
            self._buffer[self._buffered : self._buffered + count] = audio[:count]
            self._buffered += count
            offset = count
            if self._buffered == _WINDOW:
                self._score(self._buffer, _WINDOW)
                self._buffered = 0
        while offset + _WINDOW <= len(audio):
            self._score(audio[offset : offset + _WINDOW], _WINDOW)
            offset += _WINDOW
        if offset < len(audio):
            self._buffered = len(audio) - offset
            self._buffer[: self._buffered] = audio[offset:]

    def finish(self) -> list[tuple[float, float]]:
        if self._finished:
            return self.completed
        if self._buffered:
            count = self._buffered
            self._buffer[count:] = 0
            self._score(self._buffer, count)
            self._buffered = 0
        if self._start is not None and self._qualified:
            self._finish_active()
        self._commit_pending()
        self._start = None
        self._finished = True
        return self.completed

    def _active_end(self, *, preview: bool = False) -> int:
        assert self._start is not None
        if self._silence_start is None:
            end = self._processed
        elif preview:
            # Do not let an ongoing silence inflate the live preview.
            end = self._speech_end
        else:
            end = min(self._processed, self._speech_end + _PAD)
        return min(end, self._start + _MAX_SPAN)

    def _commit_pending(self) -> None:
        if self._pending is not None:
            self._completed.append(self._pending)
            self._pending = None
            if self._start is not None:
                self._start = max(self._start, self._completed[-1][1])

    def _finish_active(self) -> None:
        assert self._start is not None
        end = self._active_end()
        if end > self._start and self._speech_end > self._start:
            self._pending = (self._start, end)
        self._start = None
        self._silence_start = None
        self._qualified = False

    @torch.no_grad()
    def _score(self, frame: np.ndarray, length: int) -> None:
        probability = float(self._model(torch.from_numpy(frame), SAMPLE_RATE).item())
        frame_start = self._processed
        self._processed += length

        self._track_probability(probability, frame_start)
        self._settle(frame_start)

    def _track_probability(self, probability: float, frame_start: int) -> None:
        if probability >= 0.5:
            if self._start is None:
                self._raw_start = frame_start
                self._start = max(0, frame_start - _PAD)
                if self._completed:
                    self._start = max(self._start, self._completed[-1][1])
                self._qualified = False
            self._silence_start = None
            self._speech_end = self._processed
        elif self._start is not None and probability < 0.35:
            if self._silence_start is None:
                self._silence_start = frame_start
                self._speech_end = frame_start
        elif self._start is not None and self._silence_start is None:
            self._speech_end = self._processed

    def _settle(self, frame_start: int) -> None:

        if self._start is not None:
            if self._speech_end - self._raw_start > _MIN_SPEECH:
                self._qualified = True
            if self._pending is not None:
                gap = self._start - self._pending[1]
                if gap >= _MERGE_GAP or self._active_end() - self._pending[0] > _MAX_SPAN:
                    self._commit_pending()
                elif self._qualified:
                    self._start = self._pending[0]
                    self._pending = None

            while self._qualified and self._speech_end >= self._start + _MAX_SPAN:
                boundary = self._start + _MAX_SPAN
                self._completed.append((self._start, boundary))
                self._start = boundary
                self._raw_start = boundary

            if self._silence_start is not None and frame_start - self._silence_start >= _MIN_SILENCE:
                if self._qualified:
                    self._finish_active()
                else:
                    self._start = None
                    self._silence_start = None

        # Even with no next utterance, once its earliest possible padded start
        # cannot merge, the pending interval is immutable and ready for ASR.
        if (
            self._pending is not None
            and self._start is None
            and self._processed - _PAD - self._pending[1] >= _MERGE_GAP
        ):
            self._commit_pending()


def merge_close_spans(spans: list[tuple[float, float]], max_gap: float, max_len: float) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start - merged[-1][1] < max_gap and end - merged[-1][0] <= max_len:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
