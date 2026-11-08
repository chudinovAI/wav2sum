import logging
import os
import threading
import warnings
from dataclasses import dataclass

import numpy as np
import torch
from huggingface_hub import get_token

from wav2sum.audio import SAMPLE_RATE
from wav2sum.local_models import DIARIZATION_FILES, DIARIZATION_REVISION, snapshot_path

# pyannote enables usage telemetry by default; local calls never send telemetry.
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"

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
        prepared = snapshot_path(MODEL_ID, DIARIZATION_REVISION, DIARIZATION_FILES)
        token = None if prepared else os.environ.get("HF_TOKEN") or get_token()
        if not prepared and not token:
            raise OSError(
                "For mono or group calls, accept the pyannote terms on Hugging Face and prepare "
                "the model with a token, or set HF_TOKEN for an existing installation."
            )
        self.lock = lock or threading.Lock()
        logger.info("Loading pyannote on %s …", device)
        self.pipeline = Pipeline.from_pretrained(prepared or MODEL_ID, token=token)
        # The default Fb 0.8 makes a second person of short "okay"/"mm" in a 1-on-1 call (CHU-132).
        self.pipeline.instantiate({"clustering": {"Fb": 2.0}})
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
