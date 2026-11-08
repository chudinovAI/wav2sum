"""Owned macOS child and staging lifetime for the explicit development watcher."""

import hashlib
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from wav2sum import build as app_build
from wav2sum.build import APP_PATH, BUILD_DIR, NATIVE_DIR
from wav2sum.config import exclusive

MARKER = "WAV2SUM_DEVWATCH_PROTOCOL_V1"
MAX_LINE = 256


class WatchError(RuntimeError):
    """Unsafe or indeterminate state; never force a process or retry a swap."""


class LaunchError(WatchError):
    def __init__(self, message, *, uncertain):
        super().__init__(message)
        self.uncertain = uncertain


def _bundle_state(bundle):
    """Fingerprint verified paths; callers exclude concurrent installers/writers."""
    bundle = Path(bundle)
    if not bundle.is_absolute() or ".." in bundle.parts:
        raise WatchError("bundle path must be absolute and normalized")
    ancestors = []
    for path in reversed(bundle.parents):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise WatchError("bundle ancestor is not a directory")
        ancestors.append((info.st_dev, info.st_ino))
    entries = []
    pending = [bundle]
    while pending:
        path = pending.pop()
        info = path.lstat()
        identity = (str(path.relative_to(bundle)), info.st_dev, info.st_ino, info.st_mode, info.st_nlink)
        if stat.S_ISDIR(info.st_mode):
            entries.append((identity, None))
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            with path.open("rb") as file:
                entries.append((identity, hashlib.file_digest(file, "sha256").digest()))
        else:
            raise WatchError("bundle contains a symlink or unsafe entry")
    return tuple(ancestors), tuple(entries)


class RuntimeChild:
    def __init__(self, process):
        self.process = process
        self.stdin, self.stdout = process.stdin, process.stdout
        self._responded = False
        if self.stdin is None or self.stdout is None:
            raise WatchError("missing child control pipes")
        os.set_blocking(self.stdin.fileno(), False)
        os.set_blocking(self.stdout.fileno(), False)

    def _send(self, command, timeout):
        if command not in ("protocol", "prepare", "resume", "exit"):
            raise WatchError("invalid protocol command")
        deadline = time.monotonic() + timeout
        data = (command + "\n").encode("ascii")
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.stdin], [], remaining)[1]:
                raise WatchError("child command timeout")
            written = os.write(self.stdin.fileno(), data)
            if written <= 0:
                raise WatchError("child command failed")
            data = data[written:]

    def request(self, command, timeout):
        deadline = time.monotonic() + timeout
        if self._responded and select.select([self.stdout], [], [], 0)[0]:
            raise WatchError("unsolicited child output or EOF")
        self._send(command, timeout)
        data = b""
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.stdout], [], [], remaining)[0]:
                raise WatchError("child response timeout")
            part = os.read(self.stdout.fileno(), MAX_LINE + 1 - len(data))
            if not part:
                raise WatchError("child response EOF")
            data += part
            if len(data) > MAX_LINE:
                raise WatchError("child response too long")
        if not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise WatchError("invalid child response")
        try:
            response = data[:-1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise WatchError("invalid child response") from exc
        if not response or any(ord(c) < 32 or ord(c) > 126 for c in response):
            raise WatchError("invalid child response")
        self._responded = True
        return response

    def send_exit(self):
        self._send("exit", 5)

    def wait_exit(self, timeout):
        try:
            self.process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def alive(self):
        return self.process.poll() is None

    def detach(self):
        self.stdin.close()
        self.stdout.close()


class Runtime:
    def __init__(self):
        self.target = APP_PATH
        self.source = NATIVE_DIR / "app"
        self.serve = [sys.executable, "-m", "wav2sum", "serve"]
        self._lock = self._child = self._work = self._stage = None
        self._states = self._signature = self._work_identity = None
        self._published = self._uncertain = False

    def _verify_bundle(self, bundle):
        signature = app_build.verify_dev_app(bundle, self.serve)
        if MARKER.encode() not in (bundle / "Contents/MacOS/Wav2Sum").read_bytes():
            raise WatchError("bundle lacks dev-watch protocol marker; bootstrap required")
        return signature

    def verify_target(self):
        if sys.platform != "darwin" or self._lock is not None:
            raise WatchError("macOS and exclusive watcher ownership required")
        lock = exclusive(BUILD_DIR / "devwatch.lock", wait=False)
        if not lock.__enter__():
            lock.__exit__(None, None, None)
            raise WatchError("another watcher owns the lock")
        self._lock = lock
        try:
            app_build._check_no_app_process()
            _bundle_state(self.target)
            self._verify_bundle(self.target)
        except (OSError, app_build.BuildError) as exc:
            raise WatchError("cannot verify dev target or process state") from exc

    def installed_snapshot(self):
        signature = self._verify_bundle(self.target)
        try:
            actual = (self.target / "Contents/Resources/Wav2SumSourceDigest").read_bytes()
            snapshot = self.snapshot()
            expected = app_build.source_digest(snapshot, self.serve, signature[0]).encode()
            return snapshot if actual == expected else None
        except (OSError, app_build.BuildError):
            return None

    def snapshot(self):
        return app_build.source_snapshot(self.source)

    def build(self, snapshot):
        if self._lock is None:
            raise WatchError("watcher lock not held")
        self.discard()
        target_state = _bundle_state(self.target)
        signature = self._verify_bundle(self.target)
        self._work = Path(tempfile.mkdtemp(prefix=".wav2sum-watch-", dir=self.target.parent))
        info = self._work.lstat()
        self._work_identity = (self._work.resolve(), info.st_dev, info.st_ino)
        self._stage = self._work / "Wav2Sum.app"
        try:
            plist = (self.target / "Contents/Info.plist").read_bytes()
            app_build.build_app(snapshot, self._stage, plist, signature[0], serve=self.serve)
            if self._verify_bundle(self._stage) != signature or _bundle_state(self.target) != target_state:
                raise WatchError("app or signing identity changed during build")
            self._states = (target_state, _bundle_state(self._stage))
            self._signature = signature
            return self._stage
        except Exception as exc:
            log = self._work / "build.log"
            log.write_text(str(exc), encoding="utf-8")
            raise WatchError(f"candidate build failed; details: {log}") from exc

    def launch(self, *, cloud):
        if self._lock is None or self._uncertain or (self._child is not None and self._child.alive()):
            raise WatchError("previous child not safely reaped; no launch")
        try:
            app_build._check_no_app_process()
        except (OSError, app_build.BuildError) as exc:
            raise WatchError("app process state unknown; no launch") from exc
        try:
            _bundle_state(self.target)
            self._verify_bundle(self.target)
        except Exception as exc:
            raise LaunchError("target verification failed before launch", uncertain=False) from exc
        self._uncertain = True
        try:
            process = subprocess.Popen(
                [str(self.target / "Contents/MacOS/Wav2Sum"), *(["--cloud"] if cloud else [])],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env={**os.environ, "WAV2SUM_DEV_WATCH": "1"},
                start_new_session=True,
            )
            self._child = RuntimeChild(process)
        except Exception as exc:
            raise LaunchError("child launch uncertain; no retry", uncertain=True) from exc
        self._uncertain = False
        return self._child

    def _exchange(self):
        if self._uncertain or (self._child is not None and self._child.alive()):
            raise WatchError("child not safely reaped; no swap")
        current = (_bundle_state(self.target), _bundle_state(self._stage))
        if current != self._states:
            raise WatchError("verified bundle paths or contents changed")
        if any(self._verify_bundle(path) != self._signature for path in (self.target, self._stage)):
            raise WatchError("verified signature changed")
        if (_bundle_state(self.target), _bundle_state(self._stage)) != current:
            raise WatchError("bundle changed during verification")
        self._uncertain = True
        app_build._atomic_publish(self._stage, self.target, True)
        self._uncertain = False
        target, staged = current
        self._states = ((target[0], staged[1]), (staged[0], target[1]))

    def swap(self, staged):
        if self._stage != staged or self._published:
            raise WatchError("unverified stage or already published")
        try:
            self._exchange()
        except Exception as exc:
            raise WatchError("swap failed or uncertain; bundles retained") from exc
        self._published = True

    def rollback(self):
        if not self._published:
            raise WatchError("no known successful swap")
        try:
            self._exchange()
        except Exception as exc:
            raise WatchError("rollback failed or uncertain; bundles retained") from exc
        self._published = False

    def _cleanup(self):
        if self._work is not None:
            root = self._work
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or (root.resolve(), info.st_dev, info.st_ino) != self._work_identity:
                raise WatchError("owned workspace was substituted; not removing it")
            shutil.rmtree(root)
        self._work = self._stage = self._states = self._work_identity = None

    def discard(self):
        if self._published or self._uncertain:
            raise WatchError("recovery bundle still required")
        self._cleanup()

    def accept(self):
        """Only after the replacement's handshake or a confirmed session exit."""
        if self._uncertain:
            raise WatchError("uncertain outcome; recovery bundles retained")
        self._cleanup()
        self._published = False

    def sleep(self, seconds):
        time.sleep(seconds)

    def report(self, message):
        print(message, file=sys.stderr)

    def close(self):
        if self._work is not None:
            self.report(f"Recovery build retained: {self._work}")
        if self._lock is not None:
            lock, self._lock = self._lock, None
            lock.__exit__(None, None, None)
