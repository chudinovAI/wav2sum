import logging
import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


@dataclass
class Utterance:
    start: float
    end: float
    text: str
    speaker: str


@dataclass
class Piece:
    start: float
    end: float
    speaker: int | None


def drop_echo(
    mine: list[Utterance], theirs: list[Utterance], min_overlap: float = 0.6, slack_sec: float = 1.5
) -> list[Utterance]:
    kept = []
    for utt in mine:
        words = _words(utt.text)
        concurrent = [
            w for t in theirs if t.start - slack_sec < utt.end and t.end + slack_sec > utt.start for w in _words(t.text)
        ]
        if not (words and concurrent and _coverage(words, concurrent) >= min_overlap):
            kept.append(utt)
    if len(kept) < len(mine):
        logger.info("Dropped %d mic utterances echoed from system audio.", len(mine) - len(kept))
    return kept


def build_conversation(utterances: list[Utterance], max_gap_sec: float = 2.0) -> list[Utterance]:
    merged: list[Utterance] = []
    for utt in sorted(utterances, key=lambda u: u.start):
        prev = merged[-1] if merged else None
        if prev and prev.speaker == utt.speaker and utt.start - prev.end <= max_gap_sec:
            prev.text += " " + utt.text
            prev.end = max(prev.end, utt.end)
        else:
            merged.append(replace(utt))
    return merged


def format_transcript(utterances: list[Utterance], timestamps: bool = True) -> str:
    return "\n\n".join(
        (f"[{_clock(u.start)} – {_clock(u.end)}] " if timestamps else "") + f"{u.speaker}: {u.text}" for u in utterances
    )


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def _coverage(words: list[str], other: list[str]) -> float:
    blocks = SequenceMatcher(None, words, other, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(words)


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
