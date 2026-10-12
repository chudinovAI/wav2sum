import logging
import os
import threading
import warnings

import numpy as np
import torch

from wav2sum.audio import SAMPLE_RATE

logger = logging.getLogger(__name__)

MODEL_ID = "speechbrain/lang-id-voxlingua107-ecapa"

RU, EN = "ru", "en"
LANGUAGES = (RU, EN)
LABELS = {RU: "ru: Russian", EN: "en: English"}

MAX_SPANS = 8
MAX_CLIP_SEC = 10.0


class LanguageDetector:
    """Tells Russian from English speech: sums VoxLingua107 log-probabilities over the longest spans."""

    def __init__(self, lock: "threading.Lock | None" = None):
        os.environ.setdefault("SB_LOG_LEVEL", "ERROR")  # speechbrain reads it when it creates its loggers
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            from speechbrain.inference.classifiers import EncoderClassifier

            self.model = EncoderClassifier.from_hparams(source=MODEL_ID, savedir=None, run_opts={"device": "cpu"})
        self.lock = lock or threading.Lock()
        encoder = self.model.hparams.label_encoder
        self.indices = [encoder.encode_label(LABELS[lang]) for lang in LANGUAGES]

    def detect(self, audio: np.ndarray, spans: list[tuple[float, float]] | None = None) -> str:
        spans = spans if spans is not None else [(0.0, len(audio) / SAMPLE_RATE)]
        longest = sorted(spans, key=lambda s: s[0] - s[1])[:MAX_SPANS]
        clips = [audio[int(s * SAMPLE_RATE) : int(min(e, s + MAX_CLIP_SEC) * SAMPLE_RATE)] for s, e in longest]
        clips = [c for c in clips if len(c)]
        if not clips:
            return RU
        size = max(len(c) for c in clips)
        wavs = torch.zeros(len(clips), size)
        for row, clip in enumerate(clips):
            wavs[row, : len(clip)] = torch.from_numpy(np.ascontiguousarray(clip))
        lengths = torch.tensor([len(c) / size for c in clips])
        with self.lock, torch.inference_mode():
            log_probs = self.model.classify_batch(wavs, lengths)[0]
        scores = log_probs[:, self.indices].sum(dim=0)
        language = LANGUAGES[int(scores.argmax())]
        logger.info("Language: %s (%d spans, ru %.1f / en %.1f)", language, len(clips), *scores.tolist())
        return language
