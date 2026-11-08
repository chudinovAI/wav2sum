import itertools
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from wav2sum.audio import SAMPLE_RATE, file_hash, load_audio
from wav2sum.cloud import OpenAILLM
from wav2sum.config import AUTO
from wav2sum.context import canonicalize_terms, recognition_terms
from wav2sum.conversation import Piece, Utterance, build_conversation, drop_echo, format_transcript
from wav2sum.diarizer import Turn
from wav2sum.echo import suppress_echo
from wav2sum.language import RU, text_language
from wav2sum.llm import LLM
from wav2sum.models import Models
from wav2sum.summarizer import chapters, summarize
from wav2sum.vocabulary import Vocabulary

logger = logging.getLogger(__name__)

CACHE_VERSION = 8
CALL_WINDOW_SEC = 50.0
CALL_WINDOW_GAP_SEC = 2.0

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
        if key == SPEAKER:
            return "Спикер"
        kind, index = key.split(":")
        default = f"{self.them} {int(index) + 1}" if kind == THEM else f"Спикер {int(index) + 1}"
        return self.diarized.get(int(index), default)


@dataclass
class Result:
    out_dir: Path
    layout: str
    language: str
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
    llm: LLM | OpenAILLM | None,
    layout: str | None = None,
    speakers: Speakers | None = None,
    names: Names | None = None,
    language: str = AUTO,
    vocabulary: Vocabulary | None = None,
    dictionary: Sequence[str] = (),
    use_cache: bool = True,
    on_stage: Callable[[str], None] = lambda stage: None,
) -> Result:
    layout = layout or detect_layout(audio_path)
    speakers = speakers or Speakers()
    names = names or Names()
    vocabulary = vocabulary or Vocabulary()
    terms = recognition_terms(dictionary, vocabulary.words)
    timings = {}

    on_stage("Transcribing")
    started = time.monotonic()
    params = {
        "version": CACHE_VERSION,
        "backend": (
            "deepgram-nova-3"
            if models.cloud
            else "qwen3-asr-1.7b"
            if getattr(models, "call_model", "nemotron") == "qwen"
            else "nemotron-3.5-transcribe-30b-iq4-nl"
        ),
        "layout": layout,
        "language": language,
        "terms": list(terms),
        **asdict(speakers),
    }
    cache_file = out_root / ".cache" / file_hash(audio_path) / "utterances.json" if use_cache else None
    audio = load_audio(audio_path, channels=2 if layout == CALL else 1)

    utterances, language = _cached(
        cache_file, params, lambda: transcribe(audio, layout, speakers, models, language, terms=terms)
    )
    timings["transcription"] = time.monotonic() - started

    conversation = build_conversation(
        [
            Utterance(u.start, u.end, vocabulary.apply(canonicalize_terms(u.text, terms)), names(u.speaker))
            for u in utterances
        ]
    )

    summary = ""
    if models.cloud and llm:
        on_stage("Summarizing")
        started = time.monotonic()
        summary = summarize(format_transcript(conversation), llm, terms, language)
        if chapter_lines := chapters(format_transcript(conversation), llm, language):
            summary += "\n\n" + chapter_lines
        timings["summarization"] = time.monotonic() - started

    duration = len(audio) / SAMPLE_RATE
    result = Result(out_root / audio_path.stem, layout, language, duration, conversation, summary, timings)
    _write(result, audio_path, llm if models.cloud else None)
    return result


def summarize_existing(
    folder: Path,
    llm: OpenAILLM,
    *,
    vocabulary: Vocabulary | None = None,
    dictionary: Sequence[str] = (),
    on_stage: Callable[[str], None] = lambda stage: None,
) -> Path:
    """Add a summary atomically, without changing the saved transcript or call metadata."""
    target = folder / "summary.md"
    if os.path.lexists(target):
        if not target.is_file():
            raise ValueError("Summary output already exists and is not a file")
        return folder
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    transcript = (folder / "transcript.txt").read_text(encoding="utf-8")
    if not transcript.strip():
        raise ValueError("The saved transcript is empty")
    language = meta.get("language")
    if language not in ("ru", "en"):
        language = text_language(transcript)
    vocabulary = vocabulary or Vocabulary()
    terms = recognition_terms(dictionary, vocabulary.words)
    on_stage("Summarizing")
    summary = summarize(transcript, llm, terms, language)
    if chapter_lines := chapters(transcript, llm, language):
        summary += "\n\n" + chapter_lines
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, prefix=".summary-") as temporary:
        temporary.write(summary)
        temporary.flush()
        os.fsync(temporary.fileno())
        try:
            os.link(temporary.name, target)
        except FileExistsError:
            if not target.is_file():
                raise ValueError("Summary output already exists and is not a file") from None
    return folder


def transcribe(
    audio: np.ndarray,
    layout: str,
    speakers: Speakers,
    models: Models,
    language: str = RU,
    *,
    terms: tuple[str, ...] = (),
) -> tuple[list[Utterance], str]:
    """Transcribe one call, keeping the local worker scoped to this operation."""
    requested_language = None if language == AUTO else language
    if models.cloud:
        data = audio
        if layout == CALL:
            data = audio.copy()
            data[:, 0] = suppress_echo(audio[:, 0], audio[:, 1])
        diarize = not (speakers.without_me().just_one if layout == CALL else speakers.just_one)
        utterances = models.dictation_asr.transcribe_call(
            data, layout=layout, language=requested_language, diarize=diarize, terms=terms
        )
        if language == AUTO:
            mine = [u.text for u in utterances if u.speaker == ME]
            language = text_language(" ".join(mine or [u.text for u in utterances]))
        return utterances, language

    with models.call_session():
        if layout == MONO:
            turns = models.diarizer.diarize(audio[:, 0], speakers.count, speakers.min, speakers.max)
            return _transcribe_channel(audio[:, 0], turns, SPEAKER, models, language, terms=terms)

        mic, system = audio[:, 0], audio[:, 1]
        remote = speakers.without_me()
        turns = None if remote.just_one else models.diarizer.diarize(system, remote.count, remote.min, remote.max)
        theirs, their_language = _transcribe_channel(system, turns, THEM, models, language, terms=terms)
        mine, my_language = _transcribe_channel(suppress_echo(mic, system), None, ME, models, language, terms=terms)
        return drop_echo(mine, theirs) + theirs, my_language if mine else their_language


def _transcribe_channel(
    audio: np.ndarray, turns, key: str, models: Models, language: str, *, terms: tuple[str, ...]
) -> tuple[list[Utterance], str]:
    spans = models.vad.speech_spans(audio)
    window_sec = 20.0 if getattr(models, "call_model", "nemotron") == "qwen" else CALL_WINDOW_SEC
    pieces = _call_windows(spans, turns, max_seconds=window_sec)
    texts = (
        models.call_asr.transcribe_spans(
            audio, [(p.start, p.end) for p in pieces], language=None if language == AUTO else language, terms=terms
        )
        if pieces
        else []
    )
    if language == AUTO:
        language = text_language(" ".join(texts))
    utterances = [
        Utterance(p.start, p.end, text, key if p.speaker is None else f"{key}:{p.speaker}")
        for p, text in zip(pieces, texts, strict=True)
        if text
    ]
    return utterances, language


def _call_windows(
    spans: list[tuple[float, float]], turns: list[Turn] | None, *, max_seconds: float = CALL_WINDOW_SEC
) -> list[Piece]:
    """Bound speech windows without crossing known or ambiguous speaker transitions."""
    pieces: list[Piece] = []
    for start, end in spans:
        boundaries = {start, end}
        if turns is not None:
            for turn in turns:
                if turn.start < end and turn.end > start:
                    boundaries.add(max(start, turn.start))
                    boundaries.add(min(end, turn.end))
        ordered = sorted(boundaries)
        for left, right in itertools.pairwise(ordered):
            active = {turn.speaker for turn in turns or () if turn.start < right and turn.end > left}
            speaker = next(iter(active)) if len(active) == 1 else None
            cursor = left
            while cursor < right:
                stop = min(cursor + max_seconds, right)
                previous = pieces[-1] if pieces else None
                gap_clear = previous is not None and not any(
                    turn.speaker != speaker and turn.start < cursor and turn.end > previous.end for turn in turns or ()
                )
                if (
                    previous is not None
                    and previous.speaker == speaker
                    and 0 <= cursor - previous.end <= CALL_WINDOW_GAP_SEC
                    and stop - previous.start <= max_seconds
                    and gap_clear
                ):
                    previous.end = stop
                else:
                    pieces.append(Piece(cursor, stop, speaker))
                cursor = stop
    return pieces


def _cached(
    path: Path | None, params: dict, compute: Callable[[], tuple[list[Utterance], str]]
) -> tuple[list[Utterance], str]:
    if path and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["params"] == params:
            logger.info("Using cached transcription %s", path)
            return [Utterance(**u) for u in payload["utterances"]], payload["language"]
    utterances, language = compute()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"params": params, "language": language, "utterances": [asdict(u) for u in utterances]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return utterances, language


def _write(result: Result, audio_path: Path, llm: LLM | OpenAILLM | None) -> None:
    result.out_dir.mkdir(parents=True, exist_ok=True)
    (result.out_dir / "transcript.txt").write_text(result.transcript, encoding="utf-8")
    if result.summary:
        (result.out_dir / "summary.md").write_text(result.summary, encoding="utf-8")
    meta = {
        "audio": str(audio_path.resolve()),
        "layout": result.layout,
        "language": result.language,
        "duration_sec": round(result.duration_sec, 1),
        "speakers": list(dict.fromkeys(u.speaker for u in result.utterances)),
        "model": llm.model if llm else None,
        "timings": {k: round(v, 1) for k, v in result.timings.items()},
    }
    (result.out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
