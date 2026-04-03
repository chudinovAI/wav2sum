from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import torch
from pyannote.audio import Pipeline as PyannotePipeline

from wav2sum import detect_device

logger = logging.getLogger(__name__)

DIAR_MODEL = "pyannote/speaker-diarization-3.1"


@dataclass
class DiarSegment:
    """A speaker turn: who spoke, when."""
    speaker: int
    start: float
    end: float


class Diarizer:
    """Speaker diarization via pyannote.audio (MPS / CUDA / CPU)."""

    def __init__(self, device: str | None = None):
        self.device = detect_device(device)

        token = os.environ.get("HF_TOKEN")
        if not token:
            raise EnvironmentError(
                "HF_TOKEN is required for pyannote models.  "
                "Get yours at https://huggingface.co/settings/tokens"
            )

        logger.info("Loading pyannote diarization on %s …", self.device)
        self.pipeline = PyannotePipeline.from_pretrained(
            DIAR_MODEL, token=token,
        )
        self.pipeline.to(torch.device(self.device))
        logger.info("Diarization pipeline ready.")

    def diarize(self, audio_path: str | Path) -> list[DiarSegment]:
        """Run speaker diarization and return sorted segments."""
        audio_path = str(audio_path)
        logger.info("Diarizing %s …", audio_path)

        result = self.pipeline(audio_path)
        annotation = result.speaker_diarization

        speaker_map: dict[str, int] = {}
        segments: list[DiarSegment] = []

        for turn, _, speaker in annotation.itertracks(yield_label=True):
            if speaker not in speaker_map:
                speaker_map[speaker] = len(speaker_map)
            segments.append(DiarSegment(
                speaker=speaker_map[speaker],
                start=turn.start,
                end=turn.end,
            ))

        segments.sort(key=lambda s: s.start)
        logger.info(
            "Diarization done: %d segments, %d speakers.",
            len(segments), len({s.speaker for s in segments}),
        )
        return segments
