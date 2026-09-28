import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from wav2sum.audio import SAMPLE_RATE, file_hash, load_audio
from wav2sum.conversation import Piece, Utterance, build_conversation, drop_echo, format_transcript, split_by_turns
from wav2sum.echo import suppress_echo
from wav2sum.llm import LLM
from wav2sum.models import Models
from wav2sum.summarizer import summarize

logger = logging.getLogger(__name__)

CACHE_VERSION = 5

CALL = "call"
MONO = "mono"

ME, THEM, SPEAKER = "me", "them", "spk"


@dataclass
class Speakers:
    count: int | None = 2
    min: int | None = None
    max: int | None = None

    def without_me(self) -> "Speakers":
        def minus_one(n: int | None) -> int | None:
            return None if n is None else max(n - 1, 1)

        return Speakers(minus_one(self.count), minus_one(self.min), minus_one(self.max))

    @property
    def just_one(self) -> bool:
        return self.count == 1 or (self.count is None and self.max == 1)


@dataclass
class Names:
    me: str = "Я"
    them: str = "Собеседник"
    diarized: dict[int, str] = field(default_factory=dict)

    def __call__(self, key: str) -> str:
        if key == ME:
            return self.me
        if key == THEM:
            return self.them
        kind, index = key.split(":")
        default = f"{self.them} {int(index) + 1}" if kind == THEM else f"Спикер {int(index) + 1}"
        return self.diarized.get(int(index), default)


@dataclass
class Result:
    out_dir: Path
    layout: str
    duration_sec: float
    utterances: list[Utterance]
    summary: str
    timings: dict[str, float]

    @property
    def transcript(self) -> str:
        return format_transcript(self.utterances)


def detect_layout(audio_path: Path) -> str:
    sidecar = audio_path.with_suffix(".json")
    try:
        return CALL if json.loads(sidecar.read_text(encoding="utf-8")).get("layout") == CALL else MONO
    except (FileNotFoundError, json.JSONDecodeError):
        return MONO


def process(
    audio_path: Path,
    models: Models,
    out_root: Path,
    *,
    llm: LLM | None,
    layout: str | None = None,
    speakers: Speakers | None = None,
    names: Names | None = None,
    use_cache: bool = True,
    on_stage: Callable[[str], None] = lambda stage: None,
) -> Result:
    layout = layout or detect_layout(audio_path)
    speakers = speakers or Speakers()
    names = names or Names()
    timings = {}

    on_stage("Распознаю речь")
    started = time.monotonic()
    params = {"version": CACHE_VERSION, "layout": layout, **asdict(speakers)}
    cache_file = out_root / ".cache" / file_hash(audio_path) / "utterances.json" if use_cache else None
    audio = load_audio(audio_path, channels=2 if layout == CALL else 1)
    utterances = _cached(cache_file, params, lambda: transcribe(audio, layout, speakers, models))
    timings["transcription"] = time.monotonic() - started

    conversation = build_conversation([Utterance(u.start, u.end, u.text, names(u.speaker)) for u in utterances])

    summary = ""
    if llm:
        on_stage("Пишу саммари")
        started = time.monotonic()
        summary = summarize(format_transcript(conversation, timestamps=False), llm)
        timings["summarization"] = time.monotonic() - started

    result = Result(out_root / audio_path.stem, layout, len(audio) / SAMPLE_RATE, conversation, summary, timings)
    _write(result, audio_path, llm)
    return result


def transcribe(audio: np.ndarray, layout: str, speakers: Speakers, models: Models) -> list[Utterance]:
    if layout == MONO:
        turns = models.diarizer.diarize(audio[:, 0], speakers.count, speakers.min, speakers.max)
        return _transcribe_channel(audio[:, 0], turns, SPEAKER, models)

    mic, system = audio[:, 0], audio[:, 1]
    remote = speakers.without_me()
    turns = None if remote.just_one else models.diarizer.diarize(system, remote.count, remote.min, remote.max)
    theirs = _transcribe_channel(system, turns, THEM, models)
    mine = _transcribe_channel(suppress_echo(mic, system), None, ME, models)
    return drop_echo(mine, theirs) + theirs


def _transcribe_channel(audio: np.ndarray, turns, key: str, models: Models) -> list[Utterance]:
    spans = models.vad.speech_spans(audio)
    pieces = split_by_turns(spans, turns) if turns is not None else [Piece(s, e, None) for s, e in spans]
    texts = models.asr.transcribe_spans(audio, [(p.start, p.end) for p in pieces])
    return [
        Utterance(p.start, p.end, text, key if p.speaker is None else f"{key}:{p.speaker}")
        for p, text in zip(pieces, texts, strict=True)
        if text
    ]


def _cached(path: Path | None, params: dict, compute: Callable[[], list[Utterance]]) -> list[Utterance]:
    if path and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["params"] == params:
            logger.info("Using cached transcription %s", path)
            return [Utterance(**u) for u in payload["utterances"]]
    utterances = compute()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"params": params, "utterances": [asdict(u) for u in utterances]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return utterances


def _write(result: Result, audio_path: Path, llm: LLM | None) -> None:
    result.out_dir.mkdir(parents=True, exist_ok=True)
    (result.out_dir / "transcript.txt").write_text(result.transcript, encoding="utf-8")
    if result.summary:
        (result.out_dir / "summary.md").write_text(result.summary, encoding="utf-8")
    meta = {
        "audio": str(audio_path.resolve()),
        "layout": result.layout,
        "duration_sec": round(result.duration_sec, 1),
        "speakers": list(dict.fromkeys(u.speaker for u in result.utterances)),
        "model": llm.model if llm else None,
        "timings": {k: round(v, 1) for k, v in result.timings.items()},
    }
    (result.out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
