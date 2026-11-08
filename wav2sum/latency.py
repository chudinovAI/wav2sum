"""Dictation latency from the history: median and p90 of ASR and LLM time. Only reads the file.

uv run python -m wav2sum.latency [path]
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

from wav2sum.config import HISTORY_PATH

# Merge of CHU-107: before it the dictation LLM ran in Ollama.
MLX_SINCE = datetime.fromisoformat("2026-10-26T23:07:27+03:00")
# The dictation LLM's keep_alive: after this much idle time the model is likely unloaded.
COLD_AFTER = 600
LENGTHS = [(5, "<5 s"), (15, "5–15 s"), (30, "15–30 s"), (float("inf"), "≥30 s")]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m wav2sum.latency", description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, nargs="?", default=HISTORY_PATH)
    args = parser.parse_args(argv)
    lines = args.path.read_text(encoding="utf-8").splitlines()
    entries = [e for e in map(json.loads, filter(None, lines)) if "timings" in e]
    print("## All history\n\n" + report(entries))
    print("## Since mlx-lm\n\n" + report([e for e in entries if datetime.fromisoformat(e["time"]) >= MLX_SINCE]))


def report(entries: list[dict]) -> str:
    """Entries oldest first."""
    if not entries:
        return "No dictations.\n"
    states, previous = [], None
    for e in entries:
        time = datetime.fromisoformat(e["time"])
        states.append("cold" if previous is None or (time - previous).total_seconds() > COLD_AFTER else "warm")
        previous = time
    groups = {
        "Total": {"all": entries},
        "Length": _group(entries, [next(label for limit, label in LENGTHS if e["seconds"] < limit) for e in entries]),
        # The oldest entries were written before the history had a language.
        "Language": _group(entries, [e.get("language", "unknown") for e in entries]),
        "Mode": _group(entries, [_mode(e) for e in entries]),
        "Model state": _group(entries, states),
    }
    groups["Length"] = {label: groups["Length"][label] for _, label in LENGTHS if label in groups["Length"]}
    tables = [_table(title, rows) for title, rows in groups.items()]
    if pipeline := _pipeline_table(entries):
        tables.append(pipeline)
    return "\n".join(tables)


def _mode(e: dict) -> str:
    # Input length does not prove cleanup was skipped: even a short number or correction can need the LLM.
    if e["command"]:
        return "command"
    if e.get("translate") and e.get("language") == "ru":
        return "translate"
    return "short" if len(e["raw"].split()) <= 2 else "cleanup"


def _group(entries: list[dict], keys: list[str]) -> dict[str, list[dict]]:
    groups = {}
    for e, key in zip(entries, keys, strict=True):
        groups.setdefault(key, []).append(e)
    return groups


def _table(title: str, rows: dict[str, list[dict]]) -> str:
    lines = [f"### {title}", "", "| | n | asr p50 | asr p90 | llm p50 | llm p90 | asr share p50 |", "|---" * 7 + "|"]
    for label, entries in rows.items():
        asr = [e["timings"]["asr"] for e in entries]
        llm = [e["timings"]["llm"] for e in entries]
        share = statistics.median(a / (a + b) if a + b else 0 for a, b in zip(asr, llm, strict=True))
        lines.append(
            f"| {label} | {len(entries)} | {statistics.median(asr):.2f} | {_p90(asr):.2f} | "
            f"{statistics.median(llm):.2f} | {_p90(llm):.2f} | {share:.0%} |"
        )
    return "\n".join(lines) + "\n"


def _pipeline_table(entries: list[dict]) -> str:
    rows = []
    for provider in ("local", "cloud"):
        streamed = [
            e for e in entries if ("audio_sent" in e["timings"] if provider == "cloud" else "asr_live" in e["timings"])
        ]
        for field, title in (("finalize", "final request → ready"), ("preview", "first preview")):
            values = [e["timings"][field] for e in streamed if field in e["timings"]]
            if values:
                rows.append(_pipeline_row(f"{provider} {title}", values))
        if provider == "cloud":
            audio = [(e["timings"]["audio_sent"], e["timings"].get("audio_received", e["seconds"])) for e in streamed]
            ratios = [sent / received for sent, received in audio if received > 0]
            if ratios:
                rows.append(_pipeline_row("cloud sent / captured", ratios))
    if not rows:
        return ""
    return "\n".join(["### Dictation pipeline", "", "| Metric | n | p50 | p95 |", "|---|---|---|", *rows]) + "\n"


def _pipeline_row(label: str, values: list[float]) -> str:
    p95 = statistics.quantiles(values, n=20, method="inclusive")[-1] if len(values) > 1 else values[0]
    return f"| {label} | {len(values)} | {statistics.median(values):.2f} | {p95:.2f} |"


def _p90(values: list[float]) -> float:
    return statistics.quantiles(values, n=10, method="inclusive")[-1] if len(values) > 1 else values[0]


if __name__ == "__main__":
    main()
