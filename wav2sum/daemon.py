import asyncio
import base64
import ctypes
import functools
import hashlib
import itertools
import json
import logging
import logging.handlers
import os
import signal
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from wav2sum import __version__
from wav2sum.audio import pcm16_to_float
from wav2sum.autorecord import CallDetector, watch_inputs
from wav2sum.calls import Names, Speakers, process, summarize_existing
from wav2sum.capture import CallRecorder, CaptureError, Level, new_recording_path
from wav2sum.catalog import list_calls
from wav2sum.cloud import OpenAILLM
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
from wav2sum.history import append_dictation, load_dictations, prune_dictations
from wav2sum.llm import memory_mb
from wav2sum.models import Models
from wav2sum.vocabulary import Vocabulary

logger = logging.getLogger(__name__)

AUTO_RECORD_TICK = 1.0
HISTORY_RETENTION_INTERVAL = 60 * 60


@dataclass
class Job:
    id: int
    audio: str
    state: str = "queued"
    stage: str = ""
    out_dir: str | None = None
    error: str | None = None


@dataclass
class DictationSession:
    stream: DictationStream
    session: str | None
    step: asyncio.Task | None = None
    error: Exception | None = None
    preview: str = ""
    finishing: bool = False
    finalized_at: float = 0.0


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.models = Models(
            cloud=cfg.cloud, call_model=cfg.call_model, allow_unsafe_nemotron=cfg.allow_unsafe_nemotron
        )
        self.dictator = Dictator(
            self.models,
            cfg.dictation,
            num_ctx=4096,
            vocabulary=Vocabulary(VOCABULARY_PATH),
            language=cfg.language,
            examples=user_examples(load_dictations(EDITS_PATH)),
        )
        self.summary_llm = OpenAILLM(cfg.summary_model, num_ctx=cfg.num_ctx) if cfg.cloud else None
        self.recorder: CallRecorder | None = None
        self.auto_app: str | None = None
        self.recording_lock = asyncio.Lock()
        self.calls: CallDetector | None = None
        self.jobs: list[Job] = []
        self.job_ids = itertools.count(1)
        self.connections: set[asyncio.StreamWriter] = set()
        self.handlers: set[asyncio.Task] = set()
        self.subscribers: set[asyncio.StreamWriter] = set()
        # A dictation being spoken on each connection, with its recognition step in flight.
        self.streams: dict[asyncio.StreamWriter, DictationSession] = {}
        self.warming: asyncio.Task | None = None
        self.reconfiguring = False
        self.warm_up_error: str | None = None
        self.startup_failed = asyncio.Event()
        self.loading_models = False

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

        tasks = [
            asyncio.create_task(self._warm_up()),
            asyncio.create_task(self._work()),
            asyncio.create_task(self._maintain_history()),
        ]
        if self.cfg.auto_record:
            tasks.append(asyncio.create_task(self._watch_calls()))
        await self.stopping.wait()

        logger.info("Shutting down …")
        server.close()
        for state in self.streams.values():
            state.stream.close()
        for writer in self.connections:
            writer.close()
        await server.wait_closed()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)
        try:
            if self.recorder:
                await asyncio.to_thread(self.recorder.stop)
        finally:
            try:
                await asyncio.to_thread(self.dictator.llm.unload)
            finally:
                if self.summary_llm is not None:
                    await asyncio.to_thread(self.summary_llm.unload)
                SOCKET_PATH.unlink(missing_ok=True)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.handlers.add(asyncio.current_task())
        self.connections.add(writer)
        processing: set[asyncio.Task] = set()
        try:
            while line := await reader.readline():
                request = json.loads(line)
                if request.get("cmd") == "dictate":
                    try:
                        self._check_mode(request)
                        state = self._claim_dictation(request, writer) if request.get("streamed") else None
                    except Exception as e:
                        await _send(writer, {"id": request.get("id"), "ok": False, "error": str(e)})
                        continue
                    task = asyncio.create_task(self._reply(request, writer, state))
                    processing.add(task)
                    task.add_done_callback(processing.discard)
                else:
                    await self._reply(request, writer)
        except (ConnectionError, json.JSONDecodeError):
            pass
        finally:
            self.connections.discard(writer)
            self.subscribers.discard(writer)
            if state := self.streams.pop(writer, None):
                state.stream.close()
            for task in processing:
                task.cancel()
            if processing:
                await asyncio.gather(*processing, return_exceptions=True)
            self.handlers.discard(asyncio.current_task())
            writer.close()

    async def _reply(self, request: dict, writer: asyncio.StreamWriter, state: DictationSession | None = None) -> None:
        try:
            result = await self._dictate(request, writer, state) if state else await self._handle(request, writer)
            reply = {"ok": True, **result}
        except Exception as e:
            logger.exception("Request %s failed", request.get("cmd"))
            reply = {"ok": False, "error": str(e)}
        with suppress(ConnectionError):
            await _send(writer, {"id": request.get("id"), **reply})

    async def _handle(self, request: dict, writer: asyncio.StreamWriter) -> dict:
        self._check_mode(request)
        match request["cmd"]:
            case "status":
                return self.status()
            case "subscribe":
                self.subscribers.add(writer)
                return self.status()
            case "history":
                return await self._history()
            case "dictate_audio":
                return self._dictate_audio(request, writer)
            case "dictate_cancel":
                return self._dictate_cancel(request, writer)
            case "dictate":
                return await self._dictate(request, writer)
            case "correction":
                return self._learn(request["original"], request["edited"])
            case "record_start":
                return await self._record_start()
            case "record_stop":
                return await self._record_stop(manual=True)
            case "process":
                return {"job": asdict(self._enqueue(Path(request["path"]).expanduser()))}
            case "summarize_last_meeting":
                return await self._summarize_last_meeting()
            case "configure" | "shutdown":
                return await self._control(request)
        raise ValueError(f"unknown command {request['cmd']!r}")

    async def _control(self, request: dict) -> dict:
        if request["cmd"] == "configure":
            return await self._configure(request.get("settings"))
        self.stopping.set()
        return {}

    async def _configure(self, values: dict) -> dict:
        from wav2sum.config import apply_preferences, save_preferences, validate_settings

        values = validate_settings(values)
        async with self.recording_lock:
            busy = self.recorder or self.streams or any(job.state in ("queued", "running") for job in self.jobs)
            if (
                busy
                or self.reconfiguring
                or self.loading_models
                or (self.warming is not None and not self.warming.done())
            ):
                raise ValueError("Finish recording, dictation and processing before changing settings")
            restart = (
                (values["mode"] == "cloud") != self.cfg.cloud
                or values["call_model"] != self.cfg.call_model
                or values["cleanup_model"] != self.cfg.dictation.model
                or values.get("allow_unsafe_nemotron", False) != self.cfg.allow_unsafe_nemotron
                or self.warm_up_error is not None
            )
            self.reconfiguring = True
            try:
                await _finish_thread(save_preferences, values)
                if restart:
                    self.stopping.set()
                else:
                    apply_preferences(self.cfg, values)
                return {"settings": values, "restart": restart}
            finally:
                if not restart or not self.stopping.is_set():
                    self.reconfiguring = False

    def _check_mode(self, request: dict) -> None:
        if self.reconfiguring:
            raise ValueError("Settings are changing; reconnect after the background process restarts")
        if request.get("cmd") not in {
            "subscribe",
            "history",
            "dictate_audio",
            "dictate",
            "process",
            "summarize_last_meeting",
        }:
            return
        requested_cloud = request.get("cloud", False)
        if not isinstance(requested_cloud, bool) or requested_cloud != self.cfg.cloud:
            raise ValueError("daemon mode mismatch: reconnect using the matching local or cloud mode")
        if self.cfg.cloud and request["cmd"] not in {"subscribe", "history", "summarize_last_meeting"}:
            self.models.dictation_asr.validate_credentials()
            self.dictator.llm.validate_credentials()

    def status(self) -> dict:
        llm_mb = memory_mb()
        return {
            "version": __version__,
            "pid": os.getpid(),
            "cloud": self.cfg.cloud,
            "state": "ready" if self.ready.is_set() else "failed" if self.warm_up_error else "loading",
            # The LLM now lives in this process, so the footprint already holds it; the app and TUI add the two up.
            "memory_mb": _memory_mb() - llm_mb,
            "llm_memory_mb": llm_mb,
            "hotkey": self.cfg.dictation.hotkey,
            "auto_process": self.cfg.auto_process and (not self.cfg.cloud or self.cfg.auto_cloud_consent),
            "recordings_dir": str(self.cfg.recordings_dir.expanduser().absolute()),
            "output_dir": str(self.cfg.output_dir.expanduser().absolute()),
            "call_model": self.cfg.call_model,
            "error": self.warm_up_error,
            "recording": {"path": str(self.recorder.path), "seconds": self.recorder.seconds, "auto": self.auto_app}
            if self.recorder
            else None,
            "jobs": [asdict(j) for j in self.jobs[-20:]],
        }

    async def _history(self) -> dict:
        calls, dictations = await asyncio.to_thread(self._read_history)
        jobs = {str(Path(job.audio).resolve()): job for job in self.jobs}
        active = str(self.recorder.path.resolve()) if self.recorder else None
        for call in calls:
            if job := jobs.get(call["audio"]):
                call.update(state=job.state, stage=job.stage, error=job.error)
            if active is not None and call["audio"] == active:
                call.update(state="recording", stage="", error=None)
        return {
            "calls": calls,
            "dictations": dictations,
            "recordings_dir": str(self.cfg.recordings_dir.expanduser().absolute()),
            "output_dir": str(self.cfg.output_dir.expanduser().absolute()),
        }

    def _read_history(self) -> tuple[list[dict], list[dict]]:
        calls = []
        for entry in list_calls(self.cfg.recordings_dir, self.cfg.output_dir):
            folder = entry.path if entry.meta is not None else None
            transcript = folder / "transcript.txt" if folder else None
            complete = transcript is not None and transcript.is_file()
            summary = folder / "summary.md" if folder else None
            open_path = summary if summary and summary.is_file() else transcript if complete else folder
            meta = entry.meta or {}
            calls.append(
                {
                    "id": str(entry.path.absolute()),
                    "path": str(entry.path.absolute()),
                    "title": (entry.path.name if folder else entry.path.stem).removeprefix("call-"),
                    "audio": str(entry.audio) if entry.audio else None,
                    "out_dir": str(folder.absolute()) if folder else None,
                    "open_path": str(open_path.absolute()) if open_path else None,
                    "state": "done" if complete else "failed" if folder else "raw",
                    "stage": "",
                    "error": "Incomplete output: transcript.txt is missing" if folder and not complete else None,
                    "duration_sec": meta.get("duration_sec"),
                    "speakers": meta.get("speakers", []),
                }
            )
        dictations = []
        occurrences: dict[str, int] = {}
        for entry in load_dictations(HISTORY_PATH):
            item = {key: entry.get(key, "") for key in ("time", "text", "raw")}
            item["mode"] = (
                "command" if entry.get("command") else "translate" if entry.get("translate") else entry.get("style", "")
            )
            digest = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            ordinal = occurrences.get(digest, 0)
            occurrences[digest] = ordinal + 1
            dictations.append({"id": f"{digest}:{ordinal}", **item})
        return calls, dictations

    def _dictate_audio(self, request: dict, writer: asyncio.StreamWriter) -> dict:
        if self.warm_up_error:
            raise RuntimeError(self.warm_up_error)
        session = request.get("session")
        if session is not None and not isinstance(session, str):
            raise ValueError("dictation session must be a string")
        if request.get("first"):
            if previous := self.streams.pop(writer, None):
                previous.stream.close()
            self.streams[writer] = DictationSession(
                DictationStream(self.dictator, app=request.get("app"), title=request.get("title")), session
            )
            # The LLM unloads after 10 idle minutes; reloading while speaking takes the wait off release.
            if self.ready.is_set() and not self.dictator.llm.loaded and (self.warming is None or self.warming.done()):
                self.warming = asyncio.create_task(asyncio.to_thread(self.dictator.warm_llm))
        state = self._stream(writer, session)
        if state.finishing:
            raise RuntimeError("dictation already finishing")
        state.stream.add(pcm16_to_float(base64.b64decode(request["audio"])))
        if self.ready.is_set() and state.error is None and (state.step is None or state.step.done()):
            state.step = asyncio.create_task(self._recognize_step(writer, state))
        return {}

    async def _recognize_step(self, writer: asyncio.StreamWriter, state: DictationSession) -> None:
        try:
            while self.streams.get(writer) is state and not state.finishing:
                revision = state.stream.revision
                text = await asyncio.to_thread(state.stream.step, preview=state.session is not None)
                if self.streams.get(writer) is not state:
                    return
                if state.session is not None and text != state.preview and not writer.is_closing():
                    event = {"event": "dictation_preview", "session": state.session, "text": text}
                    writer.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                    state.preview = text
                if state.stream.cloud and state.session is not None:
                    await asyncio.sleep(0.05)
                elif state.stream.revision == revision:
                    return
        except Exception as e:
            if self.streams.get(writer) is state:
                state.error = e
                state.stream.close()
                logger.exception("Dictation recognition step failed")

    def _dictate_cancel(self, request: dict, writer: asyncio.StreamWriter) -> dict:
        state = self.streams.get(writer)
        if state is not None and state.session == request.get("session"):
            del self.streams[writer]
            state.stream.close()
        return {}

    def _stream(self, writer: asyncio.StreamWriter, session: str | None) -> DictationSession:
        if writer not in self.streams:
            raise RuntimeError("no dictation in progress on this connection")
        state = self.streams[writer]
        if state.session != session:
            raise ValueError("dictation session does not match")
        return state

    def _claim_dictation(self, request: dict, writer: asyncio.StreamWriter) -> DictationSession:
        state = self._stream(writer, request.get("session"))
        if state.finishing:
            raise RuntimeError("dictation already finishing")
        state.finishing = True
        state.finalized_at = time.monotonic()
        return state

    async def _dictate(
        self, request: dict, writer: asyncio.StreamWriter, state: DictationSession | None = None
    ) -> dict:
        started = state.finalized_at if state else time.monotonic()
        if request.get("streamed") and state is None:
            state = self._claim_dictation(request, writer)
        try:
            await self._wait_ready()
            audio = pcm16_to_float(base64.b64decode(request["audio"]))
            dictate = self.dictator.dictate
            seconds = len(audio) / 16000
            if state:
                if state.error:
                    raise state.error
                state.stream.add(audio)
                seconds = state.stream.samples / 16000
                if state.step:
                    await state.step
                if state.error:
                    raise state.error
                dictate = functools.partial(dictate, stream=state.stream)
            result = await _finish_thread(
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
            if state and self.streams.get(writer) is not state:
                raise RuntimeError("dictation cancelled")
            result.timings["finalize"] = time.monotonic() - started
        finally:
            if state:
                if self.streams.get(writer) is state:
                    del self.streams[writer]
                state.stream.close()
        entry = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app": request.get("app"),
            "seconds": round(seconds, 1),
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
            if self.reconfiguring:
                raise ValueError("Settings are changing; wait for the background process to restart")
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

    async def _record_stop(self, *, manual: bool = False) -> dict:
        if manual and self.calls and self.calls.app:
            self.calls.dismiss()
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
        if self.cfg.auto_process and (not self.cfg.cloud or self.cfg.auto_cloud_consent):
            try:
                self._enqueue(path)
            except (OSError, ValueError):
                logger.exception("Automatic processing could not be queued; recording preserved")
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

    async def _wait_ready(self) -> None:
        if not self.ready.is_set() and not self.warm_up_error:
            waiters = [asyncio.create_task(event.wait()) for event in (self.ready, self.startup_failed)]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
        if self.warm_up_error:
            raise RuntimeError(self.warm_up_error)

    async def _warm_up(self) -> None:
        self.loading_models = True
        try:
            await asyncio.to_thread(self.dictator.warm_up)
        except Exception:
            self.warm_up_error = "Selected models or cloud credentials are unavailable; open Settings"
            self.startup_failed.set()
            self._broadcast({"event": "state", "state": "failed", "error": self.warm_up_error})
            logger.exception("Loading models failed")
            return
        finally:
            self.loading_models = False
        self.ready.set()
        logger.info("Models ready (%d MB).", _memory_mb())
        self._broadcast({"event": "state", "state": "ready"})

    async def _maintain_history(self) -> None:
        while True:
            try:
                removed = await _finish_thread(prune_dictations, HISTORY_PATH)
                if removed:
                    logger.info("Removed %d dictations older than seven days", removed)
                    self._broadcast({"event": "history_pruned", "deleted": removed})
            except (OSError, ValueError) as error:
                logger.warning("Dictation retention failed (%s); history preserved", type(error).__name__)
            await asyncio.sleep(HISTORY_RETENTION_INTERVAL)

    def _existing_meeting_output(self, audio: Path) -> Path | None:
        folder = self.cfg.output_dir / audio.stem
        if not os.path.lexists(folder):
            return None
        transcript, metadata = folder / "transcript.txt", folder / "meta.json"
        if folder.is_symlink() or not folder.is_dir() or transcript.is_symlink() or metadata.is_symlink():
            raise ValueError("Incomplete or unsafe existing meeting output")
        if not transcript.is_file() or not metadata.is_file():
            raise ValueError("Incomplete existing meeting output")
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or not isinstance(meta.get("audio"), str):
            raise ValueError("Incomplete meeting metadata")
        if Path(meta["audio"]).resolve() != audio.resolve():
            raise ValueError("Ambiguous meeting output")
        return folder

    def _meeting_recordings(self) -> set[Path]:
        root = self.cfg.recordings_dir.resolve()
        candidates = set()
        for audio in self.cfg.recordings_dir.glob("*.wav"):
            if audio.is_symlink():
                raise ValueError("A meeting recording cannot be a symbolic link")
            if audio.is_file():
                candidates.add(audio.resolve())
        for metadata in self.cfg.output_dir.glob("*/meta.json"):
            if metadata.is_symlink() or metadata.parent.is_symlink():
                continue
            meta = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(meta, dict) or not isinstance(meta.get("audio"), str):
                continue
            audio = Path(meta["audio"])
            if audio.parent.resolve() == root and audio.suffix.lower() == ".wav":
                if metadata.parent.resolve() != (self.cfg.output_dir / audio.stem).resolve():
                    raise ValueError("Ambiguous meeting output folders")
                candidates.add(audio.resolve())
        return candidates

    @staticmethod
    def _meeting_time(audio: Path) -> float | None:
        sidecar = audio.with_suffix(".json")
        if sidecar.is_symlink():
            raise ValueError("A meeting sidecar cannot be a symbolic link")
        metadata = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
        if not isinstance(metadata, dict):
            raise ValueError("Invalid meeting sidecar")
        if metadata.get("layout") == "call" and "started_at" in metadata and "duration_sec" not in metadata:
            return None
        if metadata.get("started_at"):
            try:
                started = datetime.fromisoformat(metadata["started_at"])
                if started.tzinfo is None:
                    raise ValueError("Missing meeting timezone")
                return started.timestamp()
            except (TypeError, ValueError):
                raise ValueError("Invalid meeting start time") from None
        if audio.is_file():
            return audio.stat().st_mtime
        try:
            return datetime.strptime(audio.stem, "call-%Y-%m-%d_%H-%M-%S").astimezone().timestamp()
        except ValueError:
            raise FileNotFoundError("Meeting audio and recording chronology are unavailable") from None

    def _last_meeting(self) -> tuple[Path, Path | None]:
        dated = []
        for audio in self._meeting_recordings():
            timestamp = self._meeting_time(audio)
            if timestamp is not None:
                dated.append((timestamp, audio.name, audio))
        if not dated:
            raise FileNotFoundError("No completed meeting recording found")
        audio = max(dated)[2]
        return audio, self._existing_meeting_output(audio)

    async def _summarize_last_meeting(self) -> dict:
        if not self.cfg.cloud:
            raise ValueError("Meeting summaries are available only in cloud mode")
        async with self.recording_lock:
            if self.recorder is not None:
                raise ValueError("Recording is active or still finishing")
            audio, folder = await asyncio.to_thread(self._last_meeting)
            if folder is not None and (folder / "summary.md").is_file():
                return {"out_dir": str(folder), "already_done": True}
            for job in self.jobs:
                previous = Path(job.audio)
                if job.state in ("queued", "running") and previous.resolve() == audio.resolve():
                    return {"job": asdict(job)}
                if previous.stem == audio.stem and previous.resolve() != audio.resolve():
                    raise ValueError("Ambiguous meeting job")
            if folder is None:
                return {"job": asdict(self._enqueue(audio))}
            job = Job(next(self.job_ids), str(audio), out_dir=str(folder))
            self.jobs.append(job)
            self.queue.put_nowait(job)
            self._broadcast({"event": "job", **asdict(job)})
            return {"job": asdict(job)}

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
            try:
                await self._wait_ready()
                if job.out_dir is not None:
                    audio, folder = Path(job.audio), Path(job.out_dir)
                    if not self.cfg.cloud or self._existing_meeting_output(audio) != folder:
                        raise ValueError("Existing transcript is unavailable for cloud summary")
                    if self.recorder and self.recorder.path.resolve() == audio.resolve():
                        raise ValueError("Recording is active or still finishing")
                    self._update(job, state="running", stage="Summarizing")
                    out_dir = await _finish_thread(
                        summarize_existing,
                        folder,
                        self.summary_llm,
                        vocabulary=Vocabulary(VOCABULARY_PATH),
                        dictionary=self.cfg.dictation.dictionary,
                    )
                else:
                    self._validate_processing(Path(job.audio), current=job)
                    self._update(job, state="running")
                    result = await _finish_thread(
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
                        on_stage=lambda stage, job=job: self.loop.call_soon_threadsafe(
                            self._update, job, "running", stage
                        ),
                    )
                    out_dir = result.out_dir
                self._update(job, state="done", stage="", out_dir=str(out_dir))
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


async def _finish_thread(func, *args, **kwargs):
    """Let in-flight model work finish before its owner closes the client."""
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


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
