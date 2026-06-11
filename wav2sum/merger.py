from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from wav2sum.diarizer import DiarSegment
from wav2sum.transcriber import TranscriptSegment

logger = logging.getLogger(__name__)

# Split text into sentences, keeping trailing punctuation. GigaAM e2e_rnnt
# emits punctuation, so this is reliable enough to re-attribute speakers.
_SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]*", re.UNICODE)


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
    """Attribute speakers at sentence granularity.

    GigaAM packs 15–30 s of audio into one segment that can span several
    speakers, so attributing the whole segment by max overlap loses turns.
    Instead we split each segment into sentences, allocate each a time slice
    proportional to its length, and pick the speaker for that slice. Adjacent
    same-speaker sentences are collapsed back into one utterance afterwards.
    """
    if speaker_names is None:
        speaker_names = {}

    pieces: list[MergedUtterance] = []

    for tseg in transcript_segments:
        sentences = _split_sentences(tseg.text)
        total_chars = sum(len(s) for s in sentences) or 1
        seg_dur = max(tseg.end - tseg.start, 1e-6)

        cursor = tseg.start
        for sentence in sentences:
            slice_dur = (len(sentence) / total_chars) * seg_dur
            s_start, s_end = cursor, cursor + slice_dur
            cursor = s_end

            speaker = _best_speaker(s_start, s_end, diar_segments)
            label = (
                speaker_names.get(speaker, f"Спикер {speaker + 1}")
                if speaker is not None
                else "Неизвестный"
            )
            pieces.append(MergedUtterance(
                speaker=label, text=sentence, start=s_start, end=s_end,
            ))

    return _collapse_consecutive(pieces)


def format_transcript(utterances: list[MergedUtterance], timestamps: bool = True) -> str:
    """Produce a human-readable transcript."""
    lines: list[str] = []
    for utt in utterances:
        ts = f"[{_fmt(utt.start)} – {_fmt(utt.end)}] " if timestamps else ""
        lines.append(f"{ts}{utt.speaker}: {utt.text}")
    return "\n\n".join(lines)


# ── helpers ──────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    """Split into sentences; fall back to the whole text if none match."""
    sentences = [m.group().strip() for m in _SENTENCE_RE.finditer(text)]
    sentences = [s for s in sentences if s]
    return sentences or [text.strip()]


def _best_speaker(start: float, end: float, diar_segments: list[DiarSegment]) -> int | None:
    """Pick the speaker with the most overlap over [start, end]."""
    best_speaker: int | None = None
    best_overlap = 0.0
    for dseg in diar_segments:
        if dseg.end < start:
            continue
        if dseg.start > end:
            break
        ov = _overlap(start, end, dseg.start, dseg.end)
        if ov > best_overlap:
            best_overlap = ov
            best_speaker = dseg.speaker
    return best_speaker


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
