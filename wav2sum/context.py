"""Bounded vocabulary hints shared by recognition backends."""

import re
from collections.abc import Iterable, Sequence
from unicodedata import category

from wav2sum.vocabulary import TRANSLIT

MAX_TERMS = 50
MAX_CONTEXT_BYTES = 500
_MIXED_WORD = re.compile(
    r"\b(?=[A-Za-zА-Яа-яЁё0-9_-]*[А-Яа-яЁё])"
    r"(?=[A-Za-zА-Яа-яЁё0-9_-]*[A-Za-z])[A-Za-zА-Яа-яЁё0-9_-]+\b"
)


def recognition_terms(*sources: Iterable[str]) -> tuple[str, ...]:
    """Keep the first spelling of each safe term, preserving source priority."""
    result: list[str] = []
    seen: set[str] = set()
    size = 0
    for source in sources:
        for value in source:
            if not isinstance(value, str) or any(
                category(char).startswith("C") or char in "\u2028\u2029" for char in value
            ):
                continue
            term = value.strip()
            key = term.casefold()
            if not term or key in seen:
                continue
            encoded_size = len(term.encode("utf-8")) + (2 if result else 0)
            if size + encoded_size > MAX_CONTEXT_BYTES:
                continue
            result.append(term)
            seen.add(key)
            size += encoded_size
            if len(result) == MAX_TERMS:
                return tuple(result)
    return tuple(result)


def canonicalize_terms(text: str, terms: Sequence[str]) -> str:
    """Fix mixed-script spelling only when exact transliteration names a supplied term."""
    if not terms:
        return text
    spellings = {term.casefold(): term for term in terms}

    def replace(match: re.Match[str]) -> str:
        word = match.group()
        key = "".join(TRANSLIT.get(char, char) for char in word.casefold())
        return spellings.get(key, word)

    return _MIXED_WORD.sub(replace, text)
