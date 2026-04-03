from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from transformers import AutoModel

from wav2sum import detect_device

logger = logging.getLogger(__name__)

GIGAAM_MODEL_ID = "ai-sage/GigaAM-v3"
GIGAAM_REVISION = "e2e_rnnt"


@dataclass
class TranscriptSegment:
    """A chunk of transcribed text with time boundaries."""
    text: str
    start: float
    end: float


class Transcriber:
    """Speech-to-text using GigaAM V3 (e2e_rnnt — with punctuation)."""

    def __init__(
        self,
        model_id: str = GIGAAM_MODEL_ID,
        revision: str = GIGAAM_REVISION,
        device: str | None = None,
    ):
        self.device = detect_device(device)

        logger.info("Loading GigaAM %s (rev=%s) on %s …", model_id, revision, self.device)
        self.model = AutoModel.from_pretrained(
            model_id, revision=revision, trust_remote_code=True,
        )
        self.model.model.to(self.device)
        logger.info("GigaAM loaded.")

    def transcribe(self, audio_path: str | Path) -> list[TranscriptSegment]:
        """Transcribe audio → list of timed segments."""
        audio_path = str(audio_path)
        logger.info("Transcribing %s …", audio_path)

        raw_segments = self.model.transcribe_longform(audio_path)

        segments: list[TranscriptSegment] = []
        for seg in raw_segments:
            text = seg["transcription"].strip()
            if not text:
                continue
            start, end = seg["boundaries"]
            segments.append(TranscriptSegment(text=text, start=start, end=end))

        logger.info("Transcription done: %d segments.", len(segments))
        return segments
