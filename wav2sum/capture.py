import json
import logging
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wav2sum.build import capture_helper

logger = logging.getLogger(__name__)

SILENT_DB = -100


class CaptureError(RuntimeError):
    pass


@dataclass
class Level:
    seconds: float
    mic_db: float
    sys_db: float


def list_mics() -> str:
    return subprocess.run([str(capture_helper()), "--list-devices"], capture_output=True, text=True, check=True).stdout


def new_recording_path(recordings_dir: Path) -> Path:
    return recordings_dir / f"call-{datetime.now():%Y-%m-%d_%H-%M-%S}.wav"


class CallRecorder:
    def __init__(self, path: Path, mic: str | None = None, on_level: Callable[[Level], None] = lambda level: None):
        self.path = path
        self.mic = mic
        self.on_level = on_level
        self.seconds = 0.0
        self.mic_name: str | None = None
        self.started_at: datetime | None = None
        self._peaks = [-120.0, -120.0]
        self._error: str | None = None
        self._started = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [str(capture_helper()), str(self.path)] + (["--mic", self.mic] if self.mic else [])
        self._proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()
        self._started.wait(timeout=15)
        if self._error or not self.started_at:
            self._proc.kill()
            raise CaptureError(self._error or "wav2sum-capture did not start")

    def wait(self) -> None:
        self._proc.wait()

    def stop(self) -> Path:
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._reader.join(timeout=5)
        if self._error or self._proc.returncode != 0 or not self.path.exists():
            raise CaptureError(self._error or f"wav2sum-capture exited with code {self._proc.returncode}")

        sidecar = {
            "layout": "call",
            "channels": ["mic", "system"],
            "mic": self.mic_name,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "duration_sec": round(self.seconds, 1),
        }
        self.path.with_suffix(".json").write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
        self._warn_if_silent()
        return self.path

    def _read_events(self) -> None:
        try:
            self._handle_events()
        finally:
            self._started.set()

    def _handle_events(self) -> None:
        for line in self._proc.stderr:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("capture: %s", line.rstrip())
                continue
            match event.get("event"):
                case "started":
                    self.mic_name = event.get("mic")
                    self.started_at = datetime.now().astimezone()
                    self._started.set()
                case "level":
                    self.seconds = event["t"]
                    self._peaks = [max(self._peaks[0], event["mic_db"]), max(self._peaks[1], event["sys_db"])]
                    self.on_level(Level(event["t"], event["mic_db"], event["sys_db"]))
                case "stopped":
                    self.seconds = event.get("seconds", self.seconds)
                case "warning":
                    logger.warning("capture: %s", event.get("message"))
                case "error":
                    self._error = event.get("message", "unknown error")
                    self._started.set()

    def _warn_if_silent(self) -> None:
        if self.seconds < 5:
            return
        if self._peaks[1] <= SILENT_DB:
            logger.warning(
                "System audio was silent for the whole recording. If you could hear the other side, grant access: "
                "System Settings → Privacy & Security → Screen & System Audio Recording → "
                "System Audio Recording Only → wav2sum-capture."
            )
        if self._peaks[0] <= SILENT_DB:
            logger.warning(
                "The microphone was silent for the whole recording. "
                "Check access: System Settings → Privacy & Security → Microphone."
            )
