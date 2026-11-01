import asyncio
import base64
import ctypes
import functools
import itertools
import json
import logging
import logging.handlers
import os
import signal
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from wav2sum import __version__
from wav2sum.audio import pcm16_to_float
from wav2sum.autorecord import CallDetector, watch_inputs
from wav2sum.calls import Names, Speakers, process
from wav2sum.capture import CallRecorder, CaptureError, Level, new_recording_path
from wav2sum.config import (
    EDITS_PATH,
    HISTORY_PATH,
    LOG_PATH,
    MAX_MESSAGE,
    SOCKET_PATH,
    STATE_DIR,
    VOCABULARY_PATH,
    Config,
    write_default_config,
)
from wav2sum.dictation import DictationStream, Dictator, user_examples
from wav2sum.history import append_dictation, load_dictations
from wav2sum.llm import LLM, memory_mb
from wav2sum.models import Models
from wav2sum.vocabulary import Vocabulary

logger = logging.getLogger(__name__)

AUTO_RECORD_TICK = 1.0


@dataclass
class Job:
    id: int
    audio: str
    state: str = "queued"
    stage: str = ""
    out_dir: str | None = None
    error: str | None = None


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.models = Models()
        shared = cfg.model == cfg.dictation.model
        self.dictator = Dictator(
            self.models,
            cfg.dictation,
            num_ctx=cfg.num_ctx if shared else 4096,
            vocabulary=Vocabulary(VOCABULARY_PATH),
            language=cfg.language,
            examples=user_examples(load_dictations(EDITS_PATH)),
        )
        keep_alive = self.dictator.llm.keep_alive if shared else 60
        self.summary_llm = LLM(cfg.model, num_ctx=cfg.num_ctx, think=True, keep_alive=keep_alive)
        self.recorder: CallRecorder | None = None
        self.auto_app: str | None = None
        self.recording_lock = asyncio.Lock()
        self.calls: CallDetector | None = None
        self.jobs: list[Job] = []
        self.job_ids = itertools.count(1)
        self.connections: set[asyncio.StreamWriter] = set()
        self.subscribers: set[asyncio.StreamWriter] = set()
        # A dictation being spoken on each connection, with its recognition step in flight.
        self.streams: dict[asyncio.StreamWriter, tuple[DictationStream, asyncio.Task | None]] = {}
        self.warming: asyncio.Task | None = None

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.queue: asyncio.Queue[Job] = asyncio.Queue()

        if await is_running():
            raise SystemExit("wav2sum daemon is already running")
        SOCKET_PATH.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._serve, path=str(SOCKET_PATH), limit=MAX_MESSAGE)
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, self.stopping.set)
        logger.info("wav2sum %s daemon on %s (pid %d)", __version__, SOCKET_PATH, os.getpid())

        tasks = [asyncio.create_task(self._warm_up()), asyncio.create_task(self._work())]
        if self.cfg.auto_record:
            tasks.append(asyncio.create_task(self._watch_calls()))
        await self.stopping.wait()

        logger.info("Shutting down …")
        server.close()
        for writer in self.connections:
            writer.close()
        await server.wait_closed()
        for task in tasks:
            task.cancel()
        if self.recorder:
            await asyncio.to_thread(self.recorder.stop)
        await asyncio.to_thread(self.dictator.llm.unload)
        SOCKET_PATH.unlink(missing_ok=True)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections.add(writer)
        try:
            while line := await reader.readline():
                request = json.loads(line)
                try:
                    reply = {"ok": True, **await self._handle(request, writer)}
                except Exception as e:
                    logger.exception("Request %s failed", request.get("cmd"))
                    reply = {"ok": False, "error": str(e)}
                await _send(writer, {"id": request.get("id"), **reply})
        except (ConnectionError, json.JSONDecodeError):
            pass
        finally:
            self.connections.discard(writer)
            self.subscribers.discard(writer)
            self.streams.pop(writer, None)
            writer.close()

    async def _handle(self, request: dict, writer: asyncio.StreamWriter) -> dict:
        match request["cmd"]:
            case "status":
                return self.status()
            case "subscribe":
                self.subscribers.add(writer)
                return self.status()
            case "dictate_audio":
                return self._dictate_audio(request, writer)
            case "dictate":
                return await self._dictate(request, writer)
            case "correction":
                return self._learn(request["original"], request["edited"])
            case "record_start":
                return await self._record_start()
            case "record_stop":
                if self.calls and self.calls.app:
                    self.calls.dismiss()
                return await self._record_stop()
            case "process":
                return {"job": asdict(self._enqueue(Path(request["path"]).expanduser()))}
            case "shutdown":
                self.stopping.set()
                return {}
            case other:
                raise ValueError(f"unknown command {other!r}")

    def status(self) -> dict:
        llm_mb = memory_mb()
        return {
            "version": __version__,
            "pid": os.getpid(),
            "state": "ready" if self.ready.is_set() else "loading",
            # The LLM now lives in this process, so the footprint already holds it; the app and TUI add the two up.
            "memory_mb": _memory_mb() - llm_mb,
            "llm_memory_mb": llm_mb,
            "hotkey": self.cfg.dictation.hotkey,
            "recording": {"path": str(self.recorder.path), "seconds": self.recorder.seconds, "auto": self.auto_app}
            if self.recorder
            else None,
            "jobs": [asdict(j) for j in self.jobs[-20:]],
        }

    def _dictate_audio(self, request: dict, writer: asyncio.StreamWriter) -> dict:
        if request.get("first"):
            self.streams[writer] = (DictationStream(self.dictator), None)
            # The LLM unloads after 10 idle minutes; reloading it while the user speaks takes the wait off release.
            if self.ready.is_set() and not self.dictator.llm.loaded and (self.warming is None or self.warming.done()):
                self.warming = asyncio.create_task(asyncio.to_thread(self.dictator.warm_llm))
        stream, step = self._stream(writer)
        stream.add(pcm16_to_float(base64.b64decode(request["audio"])))
        # One step at a time: audio that comes in meanwhile waits for the next chunk or the final `dictate`.
        if self.ready.is_set() and (step is None or step.done()):
            self.streams[writer] = (stream, asyncio.create_task(asyncio.to_thread(stream.step)))
        return {}

    def _stream(self, writer: asyncio.StreamWriter) -> tuple[DictationStream, asyncio.Task | None]:
        if writer not in self.streams:
            raise RuntimeError("no dictation in progress on this connection")
        return self.streams[writer]

    async def _dictate(self, request: dict, writer: asyncio.StreamWriter) -> dict:
        await self.ready.wait()
        audio = pcm16_to_float(base64.b64decode(request["audio"]))
        dictate = self.dictator.dictate
        if request.get("streamed"):
            stream, step = self._stream(writer)
            del self.streams[writer]
            stream.add(audio)
            if step:
                await step
            audio = stream.audio
            dictate = functools.partial(dictate, stream=stream)
        result = await asyncio.to_thread(
            dictate,
            audio,
            app=request.get("app"),
            title=request.get("title"),
            selection=request.get("selection"),
            command=request.get("command", False),
            translate=request.get("translate", False),
            before=request.get("before"),
            after=request.get("after"),
        )
        entry = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app": request.get("app"),
            "seconds": round(len(audio) / 16000, 1),
            **asdict(result),
            "timings": {k: round(v, 2) for k, v in result.timings.items()},
        }
        if result.raw:
            append_dictation(entry)
        logger.info(
            "Dictation [%s, %s] %.2fs asr + %.2fs llm: %r",
            request.get("app"),
            result.style,
            result.timings["asr"],
            result.timings["llm"],
            result.text[:80],
        )
        self._broadcast({"event": "dictation", **entry})
        return entry

    def _learn(self, original: str, edited: str) -> dict:
        if not self.cfg.dictation.learn:
            return {"learned": []}
        if dictated := next((e for e in load_dictations(HISTORY_PATH) if e["text"] == original), None):
            edit = {k: dictated.get(k) for k in ("raw", "text", "style", "language")}
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            append_dictation({"time": now, **edit, "edited": edited.strip()}, EDITS_PATH)
        learned = self.dictator.vocabulary.learn(original, edited)
        for heard, written in learned:
            logger.info("Learned from an edit: %r → %r", heard, written)
        if learned:
            self._broadcast({"event": "learned", "pairs": learned})
        return {"learned": learned}

    async def _record_start(self, auto: str | None = None) -> dict:
        async with self.recording_lock:
            if self.recorder:
                raise RuntimeError("already recording")
            path = new_recording_path(self.cfg.recordings_dir)
            recorder = CallRecorder(path, self.cfg.mic, on_level=self._on_level, app=auto)
            self.recorder, self.auto_app = recorder, auto
            try:
                await asyncio.to_thread(recorder.start)
            except BaseException:
                self.recorder, self.auto_app = None, None
                raise
        logger.info("Recording %s%s", recorder.path, f" ({auto} call)" if auto else "")
        self._broadcast({"event": "recording", "active": True, "path": str(recorder.path), "auto": auto})
        return {"path": str(recorder.path)}

    async def _record_stop(self) -> dict:
        async with self.recording_lock:
            if not self.recorder:
                raise RuntimeError("not recording")
            recorder = self.recorder
            try:
                path = await asyncio.to_thread(recorder.stop)
            except CaptureError:
                self.recorder, self.auto_app = None, None
                self._broadcast({"event": "recording", "active": False, "path": str(recorder.path)})
                raise
            self.recorder, self.auto_app = None, None
        self._broadcast({"event": "recording", "active": False, "path": str(path)})
        return {"path": str(path)}

    def _on_level(self, level: Level) -> None:
        event = {"event": "level", "seconds": level.seconds, "mic_db": level.mic_db, "sys_db": level.sys_db}
        self.loop.call_soon_threadsafe(self._broadcast, event)

    async def _watch_calls(self) -> None:
        self.calls = CallDetector(self.cfg.call_apps)
        deciding = asyncio.create_task(self._auto_record(self.calls))
        try:
            async for apps in watch_inputs():
                before = self.calls.app
                self.calls.observe(apps, time.monotonic())
                if self.calls.app != before:
                    logger.info("Call app on the microphone: %s", self.calls.app or "none")
            logger.warning("Microphone watcher exited; automatic recording is off")
        except Exception:
            logger.exception("Microphone watcher failed; automatic recording is off")
        finally:
            deciding.cancel()

    async def _auto_record(self, calls: CallDetector) -> None:
        while True:
            await asyncio.sleep(AUTO_RECORD_TICK)
            kind = None if not self.recorder else "auto" if self.auto_app else "manual"
            try:
                match calls.decide(kind, time.monotonic()):
                    case "start":
                        await self._record_start(auto=calls.app)
                    case "stop":
                        await self._record_stop()
            except Exception:
                logger.exception("Automatic recording failed")
                calls.dismiss()

    async def _warm_up(self) -> None:
        try:
            await asyncio.to_thread(self.dictator.warm_up)
        except Exception:
            logger.exception("Loading models failed")
            return
        self.ready.set()
        logger.info("Models ready (%d MB).", _memory_mb())
        self._broadcast({"event": "state", "state": "ready"})

    def _enqueue(self, audio: Path) -> Job:
        self._validate_processing(audio)
        job = Job(next(self.job_ids), str(audio))
        self.jobs.append(job)
        self.queue.put_nowait(job)
        self._broadcast({"event": "job", **asdict(job)})
        return job

    def _validate_processing(self, audio: Path, *, current: Job | None = None) -> None:
        if not audio.is_file():
            raise FileNotFoundError(audio)
        resolved = audio.resolve()
        if self.recorder and (self.recorder.path.stem == audio.stem or self.recorder.path.resolve() == resolved):
            raise ValueError("Recording is active or still finishing")
        if os.path.lexists(self.cfg.output_dir / audio.stem):
            raise ValueError(f"Output already exists for {audio.stem}")
        for recording in self.cfg.recordings_dir.glob("*.wav"):
            if recording.stem == audio.stem and recording.resolve() != resolved:
                raise ValueError(f"Ambiguous recording stem: {audio.stem}")
        for job in self.jobs:
            if job is current:
                continue
            previous = Path(job.audio)
            if previous.resolve() == resolved:
                if previous.stem != audio.stem or job.state in ("queued", "running"):
                    raise ValueError(f"Already queued or ambiguous processing path: {audio}")
            elif previous.stem == audio.stem:
                raise ValueError(f"Ambiguous processing stem: {audio.stem}")

    async def _work(self) -> None:
        while True:
            job = await self.queue.get()
            await self.ready.wait()
            try:
                self._validate_processing(Path(job.audio), current=job)
                self._update(job, state="running")
                result = await asyncio.to_thread(
                    process,
                    Path(job.audio),
                    self.models,
                    self.cfg.output_dir,
                    llm=self.summary_llm,
                    # Me plus 1–6 people on the system channel: a group call gets each of them, not one "them".
                    speakers=Speakers(None, 2, 7),
                    names=Names(self.cfg.me, self.cfg.them),
                    language=self.cfg.language,
                    vocabulary=Vocabulary(VOCABULARY_PATH),
                    dictionary=self.cfg.dictation.dictionary,
                    on_stage=lambda stage, job=job: self.loop.call_soon_threadsafe(self._update, job, "running", stage),
                )
                self._update(job, state="done", stage="", out_dir=str(result.out_dir))
            except Exception as e:
                logger.exception("Job %d failed", job.id)
                self._update(job, state="failed", error=str(e))
            if self.queue.empty():
                await asyncio.to_thread(self.models.unload, "diarizer")

    def _update(self, job: Job, state: str, stage: str | None = None, **fields) -> None:
        job.state = state
        if stage is not None:
            job.stage = stage
        for key, value in fields.items():
            setattr(job, key, value)
        self._broadcast({"event": "job", **asdict(job)})

    def _broadcast(self, event: dict) -> None:
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode()
        for writer in list(self.subscribers):
            if writer.is_closing():
                self.subscribers.discard(writer)
            else:
                writer.write(data)


async def is_running() -> bool:
    try:
        _, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    writer.close()
    return True


def serve(cfg: Config, verbose: bool = False) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    write_default_config()
    file_handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=2)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Daemon(cfg).run())


async def _send(writer: asyncio.StreamWriter, message: dict) -> None:
    writer.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
    await writer.drain()


class _RusageInfoV2(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time",
            "system_time",
            "pkg_idle_wkups",
            "interrupt_wkups",
            "pageins",
            "wired_size",
            "resident_size",
            "phys_footprint",
            "proc_start_abstime",
            "proc_exit_abstime",
            "child_user_time",
            "child_system_time",
            "child_pkg_idle_wkups",
            "child_interrupt_wkups",
            "child_pageins",
            "child_elapsed_abstime",
            "diskio_bytesread",
            "diskio_byteswritten",
        )
    ]


def _memory_mb() -> int:
    info = _RusageInfoV2()
    ctypes.CDLL("libproc.dylib").proc_pid_rusage(os.getpid(), 2, ctypes.byref(info))
    return info.phys_footprint // 2**20
