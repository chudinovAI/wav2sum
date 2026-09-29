import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterable

from wav2sum.build import capture_helper

START_AFTER = 3.0
STOP_AFTER = 10.0


class CallDetector:
    """Decides when to start and stop a recording from the apps that currently hold the microphone."""

    def __init__(self, apps: list[str], start_after: float = START_AFTER, stop_after: float = STOP_AFTER):
        self.apps = apps
        self.start_after = start_after
        self.stop_after = stop_after
        self.app: str | None = None
        self.since = time.monotonic()
        self.dismissed = False

    def observe(self, inputs: Iterable[str], now: float) -> None:
        app = next((a for a in sorted(inputs) if self.is_call_app(a)), None)
        if (app is None) != (self.app is None):
            self.since = now
        self.app = app

    def decide(self, recording: str | None, now: float) -> str | None:
        """`recording` is None, "auto" or "manual"; returns "start", "stop" or None."""
        held = now - self.since
        if self.app is None:
            if held >= self.stop_after:
                self.dismissed = False
                if recording == "auto":
                    return "stop"
        elif recording is None and not self.dismissed and held >= self.start_after:
            return "start"
        return None

    def dismiss(self) -> None:
        """The user stopped recording mid-call: don't start again until this call is over."""
        self.dismissed = True

    def is_call_app(self, bundle_id: str) -> bool:
        return any(bundle_id == a or bundle_id.startswith(a + ".") for a in self.apps)


async def watch_inputs() -> AsyncIterator[list[str]]:
    """Yields the bundle ids of apps using the microphone whenever that set changes."""
    helper = await asyncio.to_thread(capture_helper)
    proc = await asyncio.create_subprocess_exec(
        str(helper), "--watch-inputs", stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        while line := await proc.stderr.readline():
            event = json.loads(line)
            if event.get("event") == "inputs":
                yield event["apps"]
    finally:
        if proc.returncode is None:
            proc.kill()
