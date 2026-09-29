import difflib
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_PAIRS = 200
MAX_PHRASE_WORDS = 3
MIN_SIMILARITY = 0.5
TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh sch - y - e yu ya".replace("-", "").split(" "),
        strict=True,
    )
)
PUNCTUATION = ".,!?;:…«»\"'()[]—–-"


class Vocabulary:
    """Words the user fixed by hand after dictation: what was typed → what they wanted."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.pairs: dict[str, str] = {}
        if path and path.exists():
            try:
                self.pairs = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("Ignoring unreadable %s", path)

    @property
    def words(self) -> list[str]:
        return list(dict.fromkeys(self.pairs.values()))

    def learn(self, original: str, edited: str) -> list[tuple[str, str]]:
        learned = corrections(original, edited)
        for heard, written in learned:
            self.pairs.pop(heard, None)
            self.pairs[heard] = written
        if learned:
            self.pairs = dict(list(self.pairs.items())[-MAX_PAIRS:])
            self._save()
        return learned

    def apply(self, text: str) -> str:
        for heard, written in self.pairs.items():
            text = re.sub(rf"(?<!\w){re.escape(heard)}(?!\w)", written, text, flags=re.I)
        return text

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.pairs, ensure_ascii=False, indent=2), encoding="utf-8")


def corrections(original: str, edited: str) -> list[tuple[str, str]]:
    """Sound-alike fixes of terms and names that turn `original` into `edited`; ignores rewrites and style edits."""
    before, after = original.split(), edited.split()
    matcher = difflib.SequenceMatcher(a=[_bare(w) for w in before], b=[_bare(w) for w in after])
    pairs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace" or i2 - i1 > MAX_PHRASE_WORDS or j2 - j1 > MAX_PHRASE_WORDS:
            continue
        heard = " ".join(_bare(w) for w in before[i1:i2])
        written = " ".join(_bare(w) for w in after[j1:j2])
        sentence_start = j1 == 0 or after[j1 - 1].endswith((".", "!", "?", "…"))
        if heard and written and _is_term(written, sentence_start) and _sounds_alike(heard, written):
            pairs.append((heard, written))
    return pairs


def _bare(word: str) -> str:
    return word.strip(PUNCTUATION)


def _is_term(word: str, sentence_start: bool) -> bool:
    return bool(
        re.search(r"[A-Za-z]", word) or any(c.isupper() for c in word[1:]) or (word[0].isupper() and not sentence_start)
    )


def _sounds_alike(heard: str, written: str) -> bool:
    a, b = _latin(heard), _latin(written)
    if a == b:
        return heard != written
    common = len(os.path.commonprefix([heard.lower(), written.lower()]))
    if common and max(len(heard), len(written)) - common <= 2:
        return False
    return difflib.SequenceMatcher(a=a, b=b).ratio() >= MIN_SIMILARITY


def _latin(text: str) -> str:
    return "".join(TRANSLIT.get(c, c) for c in text.lower() if c.isalnum())
