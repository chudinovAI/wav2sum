import logging
import os
import threading
import warnings
from dataclasses import dataclass

import numpy as np
import torch
from huggingface_hub import get_token

from wav2sum.audio import SAMPLE_RATE

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=r"(?s).*torchcodec.*")
    from pyannote.audio import Pipeline

logger = logging.getLogger(__name__)

MODEL_ID = "pyannote/speaker-diarization-community-1"


@dataclass
class Turn:
    speaker: int
    start: float
    end: float


class Diarizer:
    def __init__(self, device: str, lock: "threading.Lock | None" = None):
        token = os.environ.get("HF_TOKEN") or get_token()
        if not token:
            raise OSError(
                "A Hugging Face token is needed for pyannote (mono recordings and group calls): "
                "set HF_TOKEN or run `hf auth login`."
            )
        self.lock = lock or threading.Lock()
        logger.info("Loading pyannote on %s …", device)
        self.pipeline = Pipeline.from_pretrained(MODEL_ID, token=token)
        self.pipeline.to(torch.device(device))

    def diarize(
        self,
        audio: np.ndarray,
        num_speakers: int | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[Turn]:
        if num_speakers is not None:
            params = {"num_speakers": num_speakers}
        else:
            params = {k: v for k, v in (("min_speakers", min_speakers), ("max_speakers", max_speakers)) if v}

        waveform = torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
        with self.lock:
            result = self.pipeline({"waveform": waveform, "sample_rate": SAMPLE_RATE}, **params)

        ids: dict[str, int] = {}
        turns = [
            Turn(ids.setdefault(label, len(ids)), segment.start, segment.end)
            for segment, _, label in result.exclusive_speaker_diarization.itertracks(yield_label=True)
        ]
        return sorted(turns, key=lambda t: t.start)
