from __future__ import annotations

import logging
from dataclasses import dataclass

from wav2sum.diarizer import DiarSegment
from wav2sum.transcriber import TranscriptSegment

logger = logging.getLogger(__name__)


@dataclass
class MergedUtterance:
    """A single utterance attributed to a speaker."""
    speaker: str
    text: str
    start: float
    end: float


def assign_speakers(
    transcript_segments: list[TranscriptSegment],
    diar_segments: list[DiarSegment],
    speaker_names: dict[int, str] | None = None,
) -> list[MergedUtterance]:
    """Assign a speaker to each transcript segment by max time overlap."""
    if speaker_names is None:
        speaker_names = {}

    utterances: list[MergedUtterance] = []

    for tseg in transcript_segments:
        best_speaker: int | None = None
        best_overlap = 0.0

        for dseg in diar_segments:
            if dseg.end < tseg.start:
                continue
            if dseg.start > tseg.end:
                break

            ov = _overlap(tseg.start, tseg.end, dseg.start, dseg.end)
            if ov > best_overlap:
                best_overlap = ov
                best_speaker = dseg.speaker

        label = (
            speaker_names.get(best_speaker, f"Спикер {best_speaker + 1}")
            if best_speaker is not None
            else "Неизвестный"
        )

        utterances.append(MergedUtterance(
            speaker=label, text=tseg.text, start=tseg.start, end=tseg.end,
        ))

    return _collapse_consecutive(utterances)


def format_transcript(utterances: list[MergedUtterance], timestamps: bool = True) -> str:
    """Produce a human-readable transcript."""
    lines: list[str] = []
    for utt in utterances:
        ts = f"[{_fmt(utt.start)} – {_fmt(utt.end)}] " if timestamps else ""
        lines.append(f"{ts}{utt.speaker}: {utt.text}")
    return "\n\n".join(lines)


# ── helpers ──────────────────────────────────────────────────

def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _collapse_consecutive(utterances: list[MergedUtterance]) -> list[MergedUtterance]:
    if not utterances:
        return utterances
    merged: list[MergedUtterance] = [utterances[0]]
    for utt in utterances[1:]:
        prev = merged[-1]
        if utt.speaker == prev.speaker:
            prev.text += " " + utt.text
            prev.end = utt.end
        else:
            merged.append(utt)
    return merged


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
