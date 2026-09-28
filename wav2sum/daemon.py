import asyncio
import base64
import ctypes
import itertools
import json
import logging
import logging.handlers
import os
import signal
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import ollama

from wav2sum import __version__
from wav2sum.audio import pcm16_to_float
from wav2sum.calls import Names, process
from wav2sum.capture import CallRecorder, Level, new_recording_path
from wav2sum.config import HISTORY_PATH, LOG_PATH, MAX_MESSAGE, SOCKET_PATH, STATE_DIR, Config, write_default_config
from wav2sum.dictation import Dictator
from wav2sum.llm import LLM
from wav2sum.models import Models

logger = logging.getLogger(__name__)


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
        self.dictator = Dictator(self.models, cfg.dictation, num_ctx=cfg.num_ctx if shared else 4096)
        self.summary_llm = LLM(cfg.model, num_ctx=cfg.num_ctx, keep_alive=-1 if shared else "1m")
        self.recorder: CallRecorder | None = None
        self.jobs: list[Job] = []
        self.job_ids = itertools.count(1)
        self.connections: set[asyncio.StreamWriter] = set()
        self.subscribers: set[asyncio.StreamWriter] = set()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.queue: asyncio.Queue[Job] = asyncio.Queue()

        if await is_running():
            raise SystemExit("wav2sum daemon уже запущен")
        SOCKET_PATH.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._serve, path=str(SOCKET_PATH), limit=MAX_MESSAGE)
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, self.stopping.set)
        logger.info("wav2sum %s daemon on %s (pid %d)", __version__, SOCKET_PATH, os.getpid())

        tasks = [asyncio.create_task(self._warm_up()), asyncio.create_task(self._work())]
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
            writer.close()

    async def _handle(self, request: dict, writer: asyncio.StreamWriter) -> dict:
        match request["cmd"]:
            case "status":
                return self.status()
            case "subscribe":
                self.subscribers.add(writer)
                return self.status()
            case "dictate":
                return await self._dictate(request)
            case "record_start":
                return await self._record_start()
            case "record_stop":
                return await self._record_stop()
            case "process":
                return {"job": asdict(self._enqueue(Path(request["path"]).expanduser()))}
            case "shutdown":
                self.stopping.set()
                return {}
            case other:
                raise ValueError(f"unknown command {other!r}")

    def status(self) -> dict:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "state": "ready" if self.ready.is_set() else "loading",
            "memory_mb": _memory_mb(),
            "llm_memory_mb": _llm_memory_mb(),
            "hotkey": self.cfg.dictation.hotkey,
            "recording": {"path": str(self.recorder.path), "seconds": self.recorder.seconds} if self.recorder else None,
            "jobs": [asdict(j) for j in self.jobs[-20:]],
        }

    async def _dictate(self, request: dict) -> dict:
        await self.ready.wait()
        audio = pcm16_to_float(base64.b64decode(request["audio"]))
        result = await asyncio.to_thread(
            self.dictator.dictate,
            audio,
            app=request.get("app"),
            title=request.get("title"),
            selection=request.get("selection"),
            command=request.get("command", False),
        )
        entry = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app": request.get("app"),
            "seconds": round(len(audio) / 16000, 1),
            **asdict(result),
            "timings": {k: round(v, 2) for k, v in result.timings.items()},
        }
        if result.raw:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
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

    async def _record_start(self) -> dict:
        if self.recorder:
            raise RuntimeError("запись уже идёт")
        recorder = CallRecorder(new_recording_path(self.cfg.recordings_dir), self.cfg.mic, on_level=self._on_level)
        await asyncio.to_thread(recorder.start)
        self.recorder = recorder
        self._broadcast({"event": "recording", "active": True, "path": str(recorder.path)})
        return {"path": str(recorder.path)}

    async def _record_stop(self) -> dict:
        if not self.recorder:
            raise RuntimeError("запись не идёт")
        recorder, self.recorder = self.recorder, None
        path = await asyncio.to_thread(recorder.stop)
        self._broadcast({"event": "recording", "active": False, "path": str(path)})
        return {"path": str(path), "job": asdict(self._enqueue(path))}

    def _on_level(self, level: Level) -> None:
        event = {"event": "level", "seconds": level.seconds, "mic_db": level.mic_db, "sys_db": level.sys_db}
        self.loop.call_soon_threadsafe(self._broadcast, event)

    async def _warm_up(self) -> None:
        await asyncio.to_thread(self.dictator.warm_up)
        self.ready.set()
        logger.info("Models ready (%d MB).", _memory_mb())
        self._broadcast({"event": "state", "state": "ready"})

    def _enqueue(self, audio: Path) -> Job:
        if not audio.exists():
            raise FileNotFoundError(audio)
        job = Job(next(self.job_ids), str(audio))
        self.jobs.append(job)
        self.queue.put_nowait(job)
        self._broadcast({"event": "job", **asdict(job)})
        return job

    async def _work(self) -> None:
        while True:
            job = await self.queue.get()
            await self.ready.wait()
            self._update(job, state="running")
            try:
                result = await asyncio.to_thread(
                    process,
                    Path(job.audio),
                    self.models,
                    self.cfg.output_dir,
                    llm=self.summary_llm,
                    names=Names(self.cfg.me, self.cfg.them),
                    on_stage=lambda stage, job=job: self.loop.call_soon_threadsafe(self._update, job, "running", stage),
                )
                self._update(job, state="done", stage="", out_dir=str(result.out_dir))
            except Exception as e:
                logger.exception("Job %d failed", job.id)
                self._update(job, state="failed", error=str(e))

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


def _llm_memory_mb() -> int:
    try:
        return sum(m.size for m in ollama.ps().models) // 2**20
    except Exception:
        return 0
