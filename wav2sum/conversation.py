import itertools
import logging
import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from wav2sum.diarizer import Turn

logger = logging.getLogger(__name__)

MIN_PIECE_SEC = 0.4


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


def split_by_turns(spans: list[tuple[float, float]], turns: list[Turn]) -> list[Piece]:
    pieces: list[Piece] = []
    for start, end in spans:
        inside = [
            Piece(max(start, t.start), min(end, t.end), t.speaker) for t in turns if t.end > start and t.start < end
        ]
        if not inside:
            pieces.append(Piece(start, end, _nearest_speaker((start + end) / 2, turns)))
            continue
        inside[0].start = start
        inside[-1].end = end
        for a, b in itertools.pairwise(inside):
            a.end = b.start = (a.end + b.start) / 2
        pieces += _absorb_slivers(_join_same_speaker(inside))
    return pieces


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


def _nearest_speaker(t: float, turns: list[Turn]) -> int | None:
    if not turns:
        return None
    return min(turns, key=lambda turn: max(turn.start - t, t - turn.end, 0.0)).speaker


def _join_same_speaker(pieces: list[Piece]) -> list[Piece]:
    joined: list[Piece] = []
    for p in pieces:
        if joined and joined[-1].speaker == p.speaker:
            joined[-1].end = p.end
        else:
            joined.append(p)
    return joined


def _absorb_slivers(pieces: list[Piece]) -> list[Piece]:

    def duration(p: Piece) -> float:
        return p.end - p.start

    while len(pieces) > 1:
        i = min(range(len(pieces)), key=lambda k: duration(pieces[k]))
        if duration(pieces[i]) >= MIN_PIECE_SEC:
            break
        left = pieces[i - 1] if i > 0 else None
        right = pieces[i + 1] if i + 1 < len(pieces) else None
        if right is None or (left is not None and duration(left) >= duration(right)):
            left.end = pieces[i].end
        else:
            right.start = pieces[i].start
        del pieces[i]
        pieces = _join_same_speaker(pieces)
    return pieces


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def _coverage(words: list[str], other: list[str]) -> float:
    blocks = SequenceMatcher(None, words, other, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(words)


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
