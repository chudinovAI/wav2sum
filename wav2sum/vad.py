import threading

import numpy as np
import torch
from silero_vad import get_speech_timestamps, load_silero_vad

from wav2sum.asr import MAX_SPAN_SEC
from wav2sum.audio import SAMPLE_RATE


class VoiceActivityDetector:
    def __init__(self, lock: "threading.Lock | None" = None):
        self.lock = lock or threading.Lock()
        self.model = load_silero_vad()

    def speech_spans(self, audio: np.ndarray, merge_gap_sec: float = 0.6) -> list[tuple[float, float]]:
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


def merge_close_spans(spans: list[tuple[float, float]], max_gap: float, max_len: float) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start - merged[-1][1] < max_gap and end - merged[-1][0] <= max_len:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
