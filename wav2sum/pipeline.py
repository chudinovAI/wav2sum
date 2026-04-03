from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from wav2sum.audio_utils import ensure_wav_16k_mono, get_audio_duration
from wav2sum.diarizer import DiarSegment, Diarizer
from wav2sum.merger import MergedUtterance, assign_speakers, format_transcript
from wav2sum.summarizer import Summarizer
from wav2sum.transcriber import TranscriptSegment, Transcriber

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    audio_path: str
    duration_sec: float
    transcript_segments: list[TranscriptSegment]
    diar_segments: list[DiarSegment]
    utterances: list[MergedUtterance]
    transcript_text: str
    summary: str
    timings: dict[str, float] = field(default_factory=dict)


class Wav2SummaryPipeline:
    """audio → transcript with speakers → summary"""

    def __init__(
        self,
        ollama_model: str = "gemma4:26b-a4b-it-q4_K_M",
        device: str | None = None,
    ):
        self.transcriber = Transcriber(device=device)
        self.diarizer = Diarizer(device=device)
        self.summarizer = Summarizer(model=ollama_model)

    def run(
        self,
        audio_path: str | Path,
        speaker_names: dict[int, str] | None = None,
        output_dir: str | Path | None = None,
        show_timestamps: bool = True,
    ) -> PipelineResult:
        audio_path = Path(audio_path)
        timings: dict[str, float] = {}

        t0 = time.time()
        wav_path = ensure_wav_16k_mono(audio_path)
        duration = get_audio_duration(wav_path)
        timings["preprocess"] = time.time() - t0
        logger.info("Audio: %.1f sec", duration)

        t0 = time.time()
        diar_segments = self.diarizer.diarize(wav_path)
        timings["diarization"] = time.time() - t0

        t0 = time.time()
        transcript_segments = self.transcriber.transcribe(wav_path)
        timings["transcription"] = time.time() - t0

        t0 = time.time()
        utterances = assign_speakers(transcript_segments, diar_segments, speaker_names)
        transcript_text = format_transcript(utterances, timestamps=show_timestamps)
        timings["merge"] = time.time() - t0

        t0 = time.time()
        summary = self.summarizer.summarize(transcript_text)
        timings["summarization"] = time.time() - t0

        logger.info(
            "Timings: %s",
            " | ".join(f"{k}: {v:.1f}s" for k, v in timings.items()),
        )

        return PipelineResult(
            audio_path=str(audio_path),
            duration_sec=duration,
            transcript_segments=transcript_segments,
            diar_segments=diar_segments,
            utterances=utterances,
            transcript_text=transcript_text,
            summary=summary,
            timings=timings,
        )
