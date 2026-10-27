"""Numbers and names in a fresh summary of real calls that the transcript doesn't have. Only reads the calls.

Run it after a change to the summary prompt or model:
    uv run python -m wav2sum.factcheck [call folder ...]
"""

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path

from wav2sum.llm import LLM
from wav2sum.summarizer import summarize

CALLS = 3
# Thousands may be split by a space or a comma: 50 000, 50,000.
NUMBER = re.compile(r"\d+(?:[  ,]\d{3}(?!\d))*(?:[.,]\d+)?")
WORD = re.compile(r"[^\W\d_][\w'’]*")
SENTENCE_END = ".!?:;—–-*•"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m wav2sum.factcheck", description=__doc__.splitlines()[0])
    parser.add_argument("calls", type=Path, nargs="*", help=f"call folders; the last {CALLS} calls by default")
    args = parser.parse_args(argv)

    from wav2sum.config import VOCABULARY_PATH, load_config
    from wav2sum.vocabulary import Vocabulary

    cfg = load_config()
    calls = args.calls or [p.parent for p in sorted(cfg.output_dir.glob("call-*/transcript.txt"))[-CALLS:]]
    # The same model, context and terms as the summary of a call in calls.py.
    terms = list(dict.fromkeys([*cfg.dictation.dictionary, *Vocabulary(VOCABULARY_PATH).words]))
    print(report(calls, LLM(cfg.model, num_ctx=cfg.num_ctx, think=True), terms))


def report(calls: list[Path], llm: LLM, terms: Sequence[str]) -> str:
    parts = []
    for call in calls:
        transcript = (call / "transcript.txt").read_text(encoding="utf-8")
        language = json.loads((call / "meta.json").read_text(encoding="utf-8"))["language"]
        summary = summarize(_untimed(transcript), llm, terms, language)
        missing = "\n\n".join(
            f"{line}\n  not in the transcript: {', '.join(facts)}" for line, facts in unsupported(summary, transcript)
        )
        parts.append(
            f"## {call.name}\n\n### Summary\n\n{summary}\n\n### Not in the transcript\n\n{missing or 'Nothing.'}\n"
        )
    return "\n".join(parts)


def unsupported(summary: str, transcript: str) -> list[tuple[str, list[str]]]:
    """Lines of the summary with numbers and names that the transcript doesn't have, and those facts."""
    transcript = _untimed(transcript)
    numbers = {_number(n) for n in NUMBER.findall(transcript)}
    words = {_word(w) for w in WORD.findall(transcript)}
    lines = []
    for line in summary.splitlines():
        text = re.sub(r"\*\*[^*]+\*\*", lambda m: " " * len(m[0]), line)
        facts = [(m.start(), m[0]) for m in NUMBER.finditer(text) if _number(m[0]) not in numbers]
        for m in WORD.finditer(text):
            name = re.sub(r"['’]s$", "", m[0])
            if _is_name(name, text[: m.start()]) and not _said(_word(name), words):
                facts.append((m.start(), name))
        if facts:
            lines.append((line, [fact for _, fact in sorted(facts)]))
    return lines


def _untimed(transcript: str) -> str:
    return re.sub(r"^\[[^\]]*\] ", "", transcript, flags=re.MULTILINE)


def _number(text: str) -> str:
    return re.sub(r"[  ,](?=\d{3})", "", text).replace(",", ".")


def _is_name(word: str, before: str) -> bool:
    # A capital at the start of a sentence says nothing, one inside the word (DeepSeek, GPU) does.
    if len(word) < 2 or word[0].islower():
        return False
    before = before.rstrip()
    return any(c.isupper() for c in word[1:]) or (bool(before) and before[-1] not in SENTENCE_END)


def _word(word: str) -> str:
    # Latin plurals and possessives: GPUs is GPU.
    word = word.lower()
    return word[:-1] if word.isascii() and len(word) > 3 and word.endswith("s") else word


def _said(word: str, words: set[str]) -> bool:
    if word.isascii():
        return word in words
    # Russian names change their ending: Илья, Илье, Ильи.
    stem = word[: max(3, len(word) - 2)]
    return any(w.startswith(stem) for w in words)


if __name__ == "__main__":
    main()
