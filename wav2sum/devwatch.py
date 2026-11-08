"""Explicit native-app watch loop; the Python daemon is never controlled here."""

import signal
import time
from contextlib import suppress

from wav2sum.devwatch_runtime import MARKER, LaunchError, Runtime, WatchError

POLL_INTERVAL = 1.0
RESPONSE_TIMEOUT = 5.0
EXIT_TIMEOUT = 10.0
_UNAVAILABLE = object()


class Watcher:
    def __init__(self, adapter, *, cloud: bool):
        self.ops = adapter
        self.cloud = cloud
        self.child = None
        self.applied = None
        self.failed = object()
        self.retry_at = 0.0
        self.retry_delay = 30.0
        self.source_unavailable = False
        self.stopped = False
        self.exit_started = False
        self.stop_requested = False

    def _stop(self, message):
        self.stopped = True
        raise WatchError(message)

    def _request(self, command, expected):
        try:
            response = self.child.request(command, RESPONSE_TIMEOUT)
        except Exception as exc:
            self._stop(f"{command} protocol failed: {exc}")
        if response not in expected:
            self._stop(f"{command} protocol returned unexpected response")
        return response

    def start(self):
        if self.stopped or self.child is not None:
            self._stop("watcher cannot start twice")
        try:
            self.ops.verify_target()
            self.applied = self.ops.installed_snapshot()
            if not self.stop_requested:
                self.child = self.ops.launch(cloud=self.cloud)
                self._request("protocol", (MARKER,))
        except Exception as exc:
            self._stop(f"boot failed or uncertain: {exc}")

    def _mark_failed(self, snapshot):
        if snapshot != self.failed:
            self.retry_delay = 30.0
        self.failed = snapshot
        self.retry_at = time.monotonic() + self.retry_delay
        self.retry_delay = min(self.retry_delay * 2, 3600.0)

    def _alive(self):
        if not self.child.alive():
            self._stop("child exited unexpectedly; no restart")

    def _snapshot(self):
        try:
            value = self.ops.snapshot()
        except Exception:
            if not self.source_unavailable:
                self.ops.report("Swift sources unavailable; waiting for a complete snapshot")
            self.source_unavailable = True
            return _UNAVAILABLE
        self.source_unavailable = False
        return value

    def _stable_snapshot(self):
        current = self._snapshot()
        while not self.stop_requested and current is not _UNAVAILABLE and current not in (self.applied, self.failed):
            self.ops.sleep(POLL_INTERVAL)
            self._alive()
            latest = self._snapshot()
            if latest == current:
                break
            current = latest
        return current

    def _prepare(self, snapshot=None):
        while True:
            self._alive()
            if snapshot is not None and (self.stop_requested or self._snapshot() != snapshot):
                return False
            try:
                if self._request("prepare", ("busy", "ready")) == "ready":
                    return snapshot is None or not self.stop_requested
            except KeyboardInterrupt:
                with suppress(Exception):
                    self._request("resume", ("resumed",))
                raise
            self.ops.sleep(POLL_INTERVAL)

    def _exit(self):
        # Once sent, timeout/interruption must never cause a blind launch or swap.
        self.exit_started = True
        try:
            self.child.send_exit()
            if not self.child.wait_exit(EXIT_TIMEOUT):
                self._stop("exit timeout or uncertain; bundles retained")
        except Exception as exc:
            self._stop(f"exit uncertain: {exc}; bundles retained")

    def _handoff(self, staged, snapshot):
        self._exit()
        try:
            self.ops.swap(staged)
        except Exception as exc:
            self._stop(f"swap failed or uncertain: {exc}; bundles retained")
        try:
            self.child = self.ops.launch(cloud=self.cloud)
        except LaunchError as exc:
            if exc.uncertain:
                self._stop(f"launch uncertain: {exc}; bundles retained")
            try:
                self.ops.rollback()
                self.child = self.ops.launch(cloud=self.cloud)
            except Exception as recovery:
                self._stop(f"recovery failed or uncertain: {recovery}; bundles retained")
            self._request("protocol", (MARKER,))
            self._alive()
            self.ops.accept()
            self._mark_failed(snapshot)
            self.exit_started = False
            self.ops.report("launch failed; previous app restored")
            return
        except Exception as exc:
            self._stop(f"launch uncertain: {exc}; bundles retained")
        self._request("protocol", (MARKER,))
        self._alive()
        self.ops.accept()
        self.applied = snapshot
        self.failed = object()
        self.retry_delay = 30.0
        self.exit_started = False

    def poll(self):
        if self.stopped or self.child is None:
            self._stop("watcher is stopped or not started")
        self._alive()
        current = self._stable_snapshot()
        if (
            self.stop_requested
            or current is _UNAVAILABLE
            or current == self.applied
            or (current == self.failed and time.monotonic() < self.retry_at)
        ):
            return
        try:
            staged = self.ops.build(current)
        except Exception as exc:
            self._mark_failed(current)
            self.ops.report(f"build failed; existing app retained; retry scheduled: {exc}")
            return
        if self._prepare(current):
            # New editor saves do not invalidate this immutable signed candidate.
            self._handoff(staged, current)
        else:
            self.ops.discard()

    def shutdown(self):
        """End only this owned dev-app session, after its work has drained."""
        if self.stopped or self.exit_started or self.child is None:
            return
        if self.child.alive():
            self._prepare()
            self._exit()
        self.ops.accept()
        self.stopped = True
        self.exit_started = False

    def close(self):
        self.stopped = True
        try:
            if self.child is not None:
                self.child.detach()
        finally:
            self.ops.close()


def run(*, cloud: bool):
    watch = Watcher(Runtime(), cloud=cloud)

    def request_stop(signum, frame):
        # Finish the bounded in-flight pipe request; a second Ctrl-C can abort.
        watch.stop_requested = True
        signal.signal(signal.SIGINT, signal.default_int_handler)

    previous = signal.signal(signal.SIGINT, request_stop)
    try:
        watch.start()
        while not watch.stop_requested:
            watch.poll()
            if not watch.stop_requested:
                watch.ops.sleep(POLL_INTERVAL)
        watch.ops.report("Stopping dev session; waiting for dictation and corrections…")
        watch.shutdown()
    except KeyboardInterrupt:
        watch.ops.report("Stop interrupted; inspect the dev app and retained bundles before restarting")
    finally:
        try:
            watch.close()
        finally:
            signal.signal(signal.SIGINT, previous)
