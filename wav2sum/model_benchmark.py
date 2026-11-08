"""Measure local inference on explicit audio or a generated, non-private utterance."""

import argparse
import importlib.metadata
import json
import math
import os
import platform
import re
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from wav2sum.local_models import CLEANUP_MODEL, CLEANUP_REVISION, QWEN_REVISION, hardware_info

SYNTHETIC_TEXT = "Please review the project before the meeting tomorrow."


def error_rate(reference, hypothesis, *, words=True):
    def normalize(text):
        text = re.sub(r"[^\w\s]", "", text.casefold())
        return text.split() if words else list("".join(text.split()))

    ref, hyp = normalize(reference), normalize(hypothesis)
    if not ref:
        return None if hyp else 0.0
    previous = list(range(len(hyp) + 1))
    for i, a in enumerate(ref, 1):
        current = [i]
        for j, b in enumerate(hyp, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1] / len(ref)


def _swap():
    try:
        value = subprocess.check_output(["sysctl", "-n", "vm.swapusage"], text=True, stderr=subprocess.DEVNULL)
        match = re.search(r"used\s*=\s*([\d.]+)([MG])", value)
        if match:
            return float(match.group(1)) * (1024 if match.group(2) == "G" else 1)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class MemorySampler:
    """Sample sum of resident sizes for the process tree; shared pages may be counted twice."""

    def __init__(self):
        self.peak_mb = 0.0
        self.samples = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()

    def _run(self):
        while not self.stop.is_set():
            try:
                output = subprocess.check_output(
                    ["ps", "-axo", "pid=,ppid=,rss="], text=True, stderr=subprocess.DEVNULL
                )
                rows = [tuple(map(int, line.split())) for line in output.splitlines() if len(line.split()) == 3]
                descendants = {os.getpid()}
                while True:
                    expanded = descendants | {pid for pid, ppid, _ in rows if ppid in descendants}
                    if expanded == descendants:
                        break
                    descendants = expanded
                self.peak_mb = max(self.peak_mb, sum(rss for pid, _, rss in rows if pid in descendants) / 1024)
                self.samples += 1
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self.stop.wait(0.2)


def _version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def summarize_timings(samples):
    if len(samples) < 3:
        raise ValueError("At least three warm timing samples are required")
    ordered = sorted(samples)
    return {
        "samples": samples,
        "median": round(statistics.median(ordered), 3),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
    }


def _timed(fn):
    start = time.perf_counter()
    result = fn()
    return result, round(time.perf_counter() - start, 3)


def benchmark(
    audio_path,
    reference,
    *,
    call_model="qwen",
    allow_unsafe_nemotron=False,
    include_text=False,
    language=None,
    repeats=3,
    cleanup_reference=None,
):
    from wav2sum.audio import SAMPLE_RATE, load_audio
    from wav2sum.dictation import CLEANUP_PROMPT_EN, CLEANUP_PROMPT_RU
    from wav2sum.llm import LLM
    from wav2sum.models import Models

    if repeats < 3:
        raise ValueError("At least three warm timing samples are required")

    audio = load_audio(audio_path, channels=1)[:, 0]
    if not len(audio):
        raise ValueError("Audio is empty")
    models = Models(cloud=False, call_model=call_model, allow_unsafe_nemotron=allow_unsafe_nemotron)
    cleanup = LLM(CLEANUP_MODEL, temperature=0, think=False)
    swap_before = _swap()
    with MemorySampler() as memory:
        with models.call_session():
            # Identical short windows make the two ASR backends comparable.
            spans = [
                (start / SAMPLE_RATE, min(start + 20 * SAMPLE_RATE, len(audio)) / SAMPLE_RATE)
                for start in range(0, len(audio), 20 * SAMPLE_RATE)
            ]

            def recognize():
                return " ".join(models.call_asr.transcribe_spans(audio, spans, language=language))

            cold_text, cold_asr = _timed(recognize)
            asr_warm_results = [_timed(recognize) for _ in range(repeats)]
            warm_text = asr_warm_results[-1][0]
        prompt = CLEANUP_PROMPT_RU if language == "ru" else CLEANUP_PROMPT_EN
        cleaned, cold_cleanup = _timed(lambda: cleanup.chat(prompt, warm_text))
        cleanup_warm_results = [_timed(lambda: cleanup.chat(prompt, warm_text)) for _ in range(repeats)]
        warm_cleaned = cleanup_warm_results[-1][0]
        cleanup.unload()
    report = {
        "hardware": hardware_info(),
        "versions": {
            **{name: _version(name) for name in ("wav2sum", "torch", "transformers", "mlx-lm", "huggingface-hub")},
            "python": platform.python_version(),
            "macos": platform.mac_ver()[0] or None,
        },
        "call_model": call_model,
        "call_model_revision": QWEN_REVISION if call_model == "qwen" else "external gguf (see installation)",
        "cleanup_model": CLEANUP_MODEL,
        "cleanup_revision": CLEANUP_REVISION,
        "audio_seconds": round(len(audio) / SAMPLE_RATE, 3),
        "seconds": {
            "asr_cold": cold_asr,
            "asr_warm": summarize_timings([elapsed for _, elapsed in asr_warm_results]),
            "cleanup_cold": cold_cleanup,
            "cleanup_warm": summarize_timings([elapsed for _, elapsed in cleanup_warm_results]),
            "warm_percentile_method": "nearest rank; few samples give an unstable p95",
            "cold_note": "First model load in this process; operating system and disk caches are not cleared.",
        },
        "quality": {
            "cold_wer": error_rate(reference, cold_text),
            "warm_wer": error_rate(reference, warm_text),
            "cold_cer": error_rate(reference, cold_text, words=False),
            "warm_cer": error_rate(reference, warm_text, words=False),
            "cleanup_wer": error_rate(cleanup_reference, warm_cleaned) if cleanup_reference is not None else None,
            "cleanup_note": (
                "Requires a separate expected edited-text reference; spoken-text WER is not cleanup quality."
            ),
        },
        "memory": {
            "peak_process_tree_rss_mib": round(memory.peak_mb, 1) if memory.samples else None,
            "samples": memory.samples,
            "swap_used_before_mib": swap_before,
            "swap_used_after_mib": _swap(),
            "workload": "Isolated call ASR followed by cleanup; call ASR is unloaded before cleanup.",
            "limitations": (
                "This is not the full app working set or a safety guarantee: dictation, diarization, "
                "other processes and concurrent models are not exercised. Sampled RSS sum includes children, "
                "may double-count shared pages and miss short peaks; swap is system-wide before/after, "
                "not process attribution; unified GPU allocations may differ."
            ),
        },
    }
    if include_text:
        report["text"] = {
            "reference": reference,
            "asr_cold": cold_text,
            "asr_warm": warm_text,
            "cleanup_cold": cleaned,
            "cleanup_warm": warm_cleaned,
        }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, help="explicit WAV to benchmark (otherwise synthesize a fixed phrase)")
    parser.add_argument("--reference", type=Path, help="UTF-8 reference text file, required with --wav")
    parser.add_argument("--cleanup-reference", type=Path, help="optional expected edited text for cleanup WER")
    parser.add_argument("--call-model", choices=("qwen", "nemotron"), default="qwen")
    parser.add_argument("--allow-unsafe-nemotron", action="store_true", help="explicitly override the memory warning")
    parser.add_argument("--include-text", action="store_true", help="include potentially private text in the report")
    parser.add_argument("--language", choices=("auto", "en", "ru"), default="auto")
    parser.add_argument("--repeats", type=int, default=3, help="number of warm runs per stage (at least 3)")
    args = parser.parse_args(argv)
    if bool(args.wav) != bool(args.reference):
        parser.error("--wav and --reference must be provided together")
    if args.repeats < 3:
        parser.error("--repeats must be at least 3")
    with tempfile.TemporaryDirectory(prefix="wav2sum-benchmark-") as tmp:
        if args.wav:
            audio, reference = args.wav, args.reference.read_text(encoding="utf-8")
        else:
            audio, reference = Path(tmp) / "synthetic.aiff", SYNTHETIC_TEXT
            subprocess.run(["say", "-o", str(audio), SYNTHETIC_TEXT], check=True, capture_output=True)
        print(
            json.dumps(
                benchmark(
                    audio,
                    reference,
                    call_model=args.call_model,
                    allow_unsafe_nemotron=args.allow_unsafe_nemotron,
                    include_text=args.include_text,
                    language=None if args.language == "auto" else args.language,
                    repeats=args.repeats,
                    cleanup_reference=args.cleanup_reference.read_text(encoding="utf-8")
                    if args.cleanup_reference
                    else None,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
