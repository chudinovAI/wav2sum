import numpy as np


class FakeVAD:
    """Batch-defined speech boundaries for tests of callers, not of Silero's streaming algorithm."""

    def __init__(self, speech_spans):
        self.speech_spans = speech_spans

    def stream(self):
        return FakeVADStream(self)


class FakeVADStream:
    def __init__(self, detector):
        self.detector = detector
        self.chunks = []

    def add(self, audio):
        self.chunks.append(audio)

    @property
    def completed(self):
        return []

    @property
    def spans(self):
        audio = np.concatenate(self.chunks) if self.chunks else np.empty(0, np.float32)
        return self.detector.speech_spans(audio)

    def finish(self):
        return self.spans
