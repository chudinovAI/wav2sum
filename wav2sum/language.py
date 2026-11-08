import re

RU, EN = "ru", "en"
MAX_LATIN_IN_RU = 0.6


def text_language(text: str) -> str:
    letters = re.findall(r"[^\W\d_]", text)
    return EN if letters and sum(c.isascii() for c in letters) / len(letters) > MAX_LATIN_IN_RU else RU
