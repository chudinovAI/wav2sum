"""Dictation quality from the history: raw text left by the cleanup, words kept, English entries. Only reads the file.

uv run python -m wav2sum.quality [path]
"""

import argparse
import json
import re
import statistics
from pathlib import Path

from wav2sum.config import HISTORY_PATH
from wav2sum.latency import _mode

TOP = 15
SHORT_SEC = 5
CLIP = 60


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m wav2sum.quality", description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, nargs="?", default=HISTORY_PATH)
    args = parser.parse_args(argv)
    lines = args.path.read_text(encoding="utf-8").splitlines()
    print(report(list(map(json.loads, filter(None, lines)))))


def report(entries: list[dict]) -> str:
    """Entries oldest first."""
    if not entries:
        return "No dictations.\n"
    return _cleanup([e for e in entries if _mode(e) == "cleanup"]) + "\n" + _english(entries)


def _cleanup(entries: list[dict]) -> str:
    lines = ["## Cleanup (more than 2 words, no command or translation)", ""]
    if not entries:
        return "\n".join(lines) + "No entries.\n"
    # Exact match: a fallback that fit_to_cursor or the vocabulary changed is not counted.
    raw = sum(e["text"].strip() == e["raw"].strip() for e in entries)
    kept = [(len(e["text"].split()) / len(e["raw"].split()), e) for e in entries]
    total = sum(len(e["text"].split()) for e in entries) / sum(len(e["raw"].split()) for e in entries)
    lines += [
        f"Raw text inserted: {raw} of {len(entries)} ({raw / len(entries):.0%})",
        f"Words kept, raw → text: {total:.0%} in total, median {statistics.median(k for k, _ in kept):.0%}",
        "",
        f"### {TOP} strongest cuts",
        "",
        "| time | words | kept | raw | text |",
        "|---" * 5 + "|",
    ]
    for share, e in sorted(kept, key=lambda k: k[0])[:TOP]:
        words = len(e["raw"].split())
        lines.append(f"| {e['time'][:16]} | {words} | {share:.0%} | {_cell(e['raw'])} | {_cell(e['text'])} |")
    return "\n".join(lines) + "\n"


def _english(entries: list[dict]) -> str:
    english = [e for e in entries if e.get("language") == "en"]
    short = [e for e in english if e["seconds"] < SHORT_SEC]
    lines = [
        "## English",
        "",
        f"English entries: {len(english)}, with Cyrillic in raw: {sum(_cyrillic(e['raw']) > 0 for e in english)}, "
        f"in text: {sum(_cyrillic(e['text']) > 0 for e in english)}, shorter than {SHORT_SEC} s: {len(short)}",
        "",
        f"### Shorter than {SHORT_SEC} s, latest {TOP}",
        "",
        "| time | s | cyrillic raw | cyrillic text | raw | text |",
        "|---" * 6 + "|",
    ]
    for e in short[-TOP:]:
        lines.append(
            f"| {e['time'][:16]} | {e['seconds']:.1f} | {_cyrillic(e['raw']):.0%} | {_cyrillic(e['text']):.0%} | "
            f"{_cell(e['raw'])} | {_cell(e['text'])} |"
        )
    return "\n".join(lines) + "\n"


def _cyrillic(text: str) -> float:
    """Share of Cyrillic among the letters."""
    letters = re.findall(r"[^\W\d_]", text)
    return sum("Ѐ" <= c <= "ӿ" for c in letters) / len(letters) if letters else 0.0


def _cell(text: str) -> str:
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= CLIP else text[: CLIP - 1] + "…"


if __name__ == "__main__":
    main()
