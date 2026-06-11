from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
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
        num_speakers: int | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        use_cache: bool = True,
    ) -> PipelineResult:
        audio_path = Path(audio_path)
        output_dir = Path(output_dir) if output_dir is not None else None
        timings: dict[str, float] = {}

        t0 = time.time()
        wav_path = ensure_wav_16k_mono(audio_path, output_dir)
        duration = get_audio_duration(wav_path)
        timings["preprocess"] = time.time() - t0
        logger.info("Audio: %.1f sec", duration)

        # Diarization and transcription are the expensive, deterministic steps;
        # cache them keyed by audio content so re-running with a different
        # summary model/prompt is near-instant.
        cache_dir = (output_dir / ".cache" / _audio_hash(wav_path)) if (output_dir and use_cache) else None
        diar_params = {"num_speakers": num_speakers, "min_speakers": min_speakers, "max_speakers": max_speakers}

        t0 = time.time()
        diar_segments = _cached(
            cache_dir, "diarization.json", DiarSegment, diar_params,
            lambda: self.diarizer.diarize(wav_path, num_speakers, min_speakers, max_speakers),
        )
        timings["diarization"] = time.time() - t0

        t0 = time.time()
        transcript_segments = _cached(
            cache_dir, "transcription.json", TranscriptSegment, None,
            lambda: self.transcriber.transcribe(wav_path),
        )
        timings["transcription"] = time.time() - t0

        t0 = time.time()
        utterances = assign_speakers(transcript_segments, diar_segments, speaker_names)
        transcript_text = format_transcript(utterances, timestamps=show_timestamps)
        # The model gets a clean, timestamp-free transcript: timestamps are pure
        # noise for summarization and waste context tokens.
        transcript_for_llm = format_transcript(utterances, timestamps=False)
        timings["merge"] = time.time() - t0

        t0 = time.time()
        summary = self.summarizer.summarize(transcript_for_llm)
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


# ── caching helpers ──────────────────────────────────────────

def _audio_hash(wav_path: Path) -> str:
    """Content hash of the 16 kHz wav, used as the cache key."""
    h = hashlib.blake2b(digest_size=16)
    with open(wav_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _cached(cache_dir, filename, cls, params, compute):
    """Return cached dataclass list if present & params match, else compute it."""
    if cache_dir is None:
        return compute()

    path = cache_dir / filename
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("params") == params:
                logger.info("Cache hit: %s", path.name)
                return [cls(**item) for item in payload["items"]]
            logger.info("Cache stale (params changed): %s", path.name)
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Ignoring corrupt cache: %s", path)

    result = compute()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"params": params, "items": [asdict(x) for x in result]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return result
