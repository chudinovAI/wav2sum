"""Orchestration tests use no real processes, bundles, or user data."""

import signal
from collections import deque
from types import SimpleNamespace

import pytest

from wav2sum.devwatch import LaunchError, Watcher, WatchError, run


class Child:
    def __init__(self, ops, name):
        self.ops = ops
        self.name = name

    def request(self, command, timeout):
        self.ops.trace.append((self.name, command))
        if command == "protocol":
            return self.ops.marker
        if command == "prepare":
            value = self.ops.prepares.popleft() if self.ops.prepares else "ready"
            if callable(value):
                return value()
            return value
        if command == "resume":
            return "resumed"
        raise AssertionError(command)

    def send_exit(self):
        self.ops.trace.append((self.name, "exit"))

    def wait_exit(self, timeout):
        self.ops.trace.append((self.name, "wait"))
        if self.ops.on_wait:
            self.ops.on_wait()
        return self.ops.exit_confirmed

    def alive(self):
        return True

    def detach(self):
        self.ops.trace.append((self.name, "detach"))


class Fake:
    def __init__(self):
        self.trace = []
        self.source = "v1"
        self.installed = "v1"
        self.marker = "WAV2SUM_DEVWATCH_PROTOCOL_V1"
        self.prepares = deque()
        self.exit_confirmed = True
        self.fail_build = False
        self.fail_snapshot = False
        self.fail_swap = False
        self.launch_error = None
        self.launches = 0
        self.on_build = None
        self.on_wait = None
        self.on_swap = None
        self.sleeps = 0
        self.on_sleep = None

    def verify_target(self):
        self.trace.append("verify")

    def installed_snapshot(self):
        return self.installed

    def snapshot(self):
        self.trace.append(("snapshot", self.source))
        if self.fail_snapshot:
            raise OSError("source temporarily unavailable")
        return self.source

    def build(self, snapshot):
        self.trace.append(("build", snapshot))
        if self.fail_build:
            raise OSError("build failed")
        if self.on_build:
            self.on_build()
        return snapshot

    def launch(self, *, cloud):
        self.launches += 1
        self.trace.append(("launch", cloud))
        if self.launches > 1 and self.launch_error:
            error, self.launch_error = self.launch_error, None
            raise error
        return Child(self, str(self.launches))

    def swap(self, staged):
        self.trace.append(("swap", staged))
        if self.on_swap:
            self.on_swap()
        if self.fail_swap:
            raise OSError("swap failed")

    def rollback(self):
        self.trace.append("rollback")

    def sleep(self, seconds):
        self.sleeps += 1
        self.trace.append(("sleep", seconds))
        if self.on_sleep:
            self.on_sleep()

    def report(self, message):
        self.trace.append(("report", message))

    def accept(self):
        self.trace.append("accept")

    def discard(self):
        self.trace.append("discard")

    def close(self):
        self.trace.append("close")


def started(fake=None):
    ops = fake or Fake()
    watch = Watcher(ops, cloud=True)
    watch.start()
    return ops, watch


def test_boot_verifies_before_exact_child_handshake():
    ops, watch = started()
    assert ops.trace[:3] == ["verify", ("launch", True), ("1", "protocol")]
    assert watch.applied == "v1"


def test_success_builds_before_prepare_and_waits_exact_child():
    ops, watch = started()
    ops.source = "v2"
    watch.poll()
    assert ops.trace.index(("build", "v2")) < ops.trace.index(("1", "prepare"))
    assert ops.trace.index(("1", "wait")) < ops.trace.index(("swap", "v2"))
    assert ops.trace.index(("swap", "v2")) < ops.trace.index(("2", "protocol"))
    assert watch.applied == "v2"


def test_busy_retries_without_timeout_or_kill_even_past_three_minutes():
    ops, watch = started()
    ops.source = "v2"
    ops.prepares.extend(["busy"] * 200 + ["ready"])
    watch.poll()
    assert ops.sleeps >= 200
    assert sum(item == ("1", "prepare") for item in ops.trace) == 201
    assert ("1", "exit") in ops.trace


def test_failed_build_keeps_app_and_waits_for_new_source():
    ops, watch = started()
    ops.source = "v2"
    ops.fail_build = True
    watch.poll()
    watch.poll()
    assert ops.trace.count(("build", "v2")) == 1
    assert ("1", "prepare") not in ops.trace
    assert ("1", "exit") not in ops.trace
    ops.fail_build = False
    ops.source = "v3"
    watch.poll()
    assert watch.applied == "v3"


def test_transient_build_failure_retries_same_source_with_backoff(monkeypatch):
    from wav2sum import devwatch

    now = [100.0]
    monkeypatch.setattr(devwatch.time, "monotonic", lambda: now[0])
    ops, watch = started()
    ops.source = "v2"
    ops.fail_build = True
    watch.poll()
    ops.fail_build = False
    watch.poll()
    assert ops.trace.count(("build", "v2")) == 1
    now[0] += 30
    watch.poll()
    assert ops.trace.count(("build", "v2")) == 2
    assert watch.applied == "v2"


def test_temporarily_missing_source_does_not_stop_watcher():
    ops, watch = started()
    ops.source = "v2"
    ops.fail_snapshot = True
    watch.poll()
    assert not watch.stopped
    assert ("1", "exit") not in ops.trace
    ops.fail_snapshot = False
    watch.poll()
    assert watch.applied == "v2"


def test_missing_source_after_ready_does_not_invalidate_compiled_snapshot():
    ops, watch = started()
    ops.source = "v2"

    def ready_then_missing():
        ops.fail_snapshot = True
        return "ready"

    ops.prepares.append(ready_then_missing)
    watch.poll()
    assert ("swap", "v2") in ops.trace
    assert watch.applied == "v2"
    watch.poll()
    assert not watch.stopped


def test_change_during_build_rebuilds_without_preparing_stale_snapshot():
    ops, watch = started()
    ops.source = "v2"
    ops.on_build = lambda: setattr(ops, "source", "v3")
    watch.poll()
    assert ("1", "prepare") not in ops.trace
    ops.on_build = None
    watch.poll()
    assert watch.applied == "v3"


def test_change_after_ready_finishes_captured_build_then_queues_newer_source():
    ops, watch = started()
    ops.source = "v2"

    def change():
        ops.source = "v3"
        return "ready"

    ops.prepares.append(change)
    watch.poll()
    assert ("1", "resume") not in ops.trace
    assert ("swap", "v2") in ops.trace
    assert watch.applied == "v2"
    watch.poll()
    assert watch.applied == "v3"


def test_exit_timeout_fails_closed_preserving_bundles():
    ops, watch = started()
    ops.source = "v2"
    ops.exit_confirmed = False
    with pytest.raises(WatchError, match="exit"):
        watch.poll()
    assert not any(isinstance(item, tuple) and item[0] == "swap" for item in ops.trace)
    assert ops.launches == 1
    with pytest.raises(WatchError):
        watch.poll()


@pytest.mark.parametrize("during", ["on_wait", "on_swap"])
def test_editor_save_during_handoff_does_not_roll_back_verified_snapshot(during):
    ops, watch = started()
    ops.source = "v2"
    setattr(ops, during, lambda: setattr(ops, "source", "v3"))
    watch.poll()
    assert ("swap", "v2") in ops.trace
    assert "rollback" not in ops.trace
    assert ops.launches == 2
    assert watch.applied == "v2"
    assert not watch.stopped


def test_swap_failure_does_not_exchange_again_or_relaunch():
    ops, watch = started()
    ops.source = "v2"
    ops.fail_swap = True
    with pytest.raises(WatchError, match="swap"):
        watch.poll()
    assert "rollback" not in ops.trace
    assert ops.launches == 1


@pytest.mark.parametrize("uncertain", [True, False])
def test_launch_failure_rollback_only_when_no_child_proven(uncertain):
    ops, watch = started()
    ops.source = "v2"
    ops.launch_error = LaunchError("launch failed", uncertain=uncertain)
    if uncertain:
        with pytest.raises(WatchError, match="launch"):
            watch.poll()
        assert "rollback" not in ops.trace
        assert ops.launches == 2
    else:
        watch.poll()
        assert "rollback" in ops.trace
        assert ops.launches == 3
        assert not watch.stopped


def test_bad_handshake_never_relaunches_or_rolls_back():
    ops, watch = started()
    ops.source = "v2"
    ops.marker = "wrong"
    with pytest.raises(WatchError, match="protocol"):
        watch.poll()
    assert watch.applied == "v1"
    assert "rollback" not in ops.trace
    assert ops.launches == 2


def test_poll_coalesces_burst_and_skips_unchanged():
    ops, watch = started()
    watch.poll()
    assert not any(isinstance(item, tuple) and item[0] == "build" for item in ops.trace)
    ops.source = "v2"
    ops.on_sleep = lambda: setattr(ops, "source", "v3")
    watch.poll()
    assert ("build", "v2") not in ops.trace
    assert ("build", "v3") in ops.trace
    assert watch.applied == "v3"


@pytest.mark.parametrize("during", ["protocol", "prepare"])
def test_first_ctrlc_drains_an_inflight_response_before_app_only_exit(monkeypatch, during):
    from wav2sum import devwatch, devwatch_runtime

    ops = Fake()
    ops.source = "v2"
    responses, commands = deque(), []
    process = SimpleNamespace(returncode=None)
    pipe = SimpleNamespace(fileno=lambda: 42, close=lambda: None)
    process.stdin = process.stdout = pipe
    process.poll = lambda: process.returncode
    process.wait = lambda timeout: process.returncode
    monkeypatch.setattr(devwatch_runtime.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(devwatch_runtime.select, "select", lambda r, w, x, timeout: (r if responses else [], w, []))
    interrupted = False

    def send(fd, data):
        command = data.decode().strip()
        commands.append(command)
        if command == "exit":
            process.returncode = 0
        else:
            reply = {"protocol": ops.marker, "prepare": "ready", "resume": "resumed"}[command]
            responses.append((reply + "\n").encode())
        return len(data)

    def receive(*args):
        nonlocal interrupted
        if commands[-1] == during and not interrupted:
            interrupted = True
            signal.raise_signal(signal.SIGINT)
        return responses.popleft()

    monkeypatch.setattr(devwatch_runtime.os, "write", send)
    monkeypatch.setattr(devwatch_runtime.os, "read", receive)
    ops.launch = lambda **kwargs: devwatch_runtime.RuntimeChild(process)
    monkeypatch.setattr(devwatch, "Runtime", lambda: ops)
    previous_handler = signal.getsignal(signal.SIGINT)
    run(cloud=False)
    assert interrupted and process.returncode == 0
    assert commands[-1] == "exit"
    assert "resume" not in commands, "do not replay a command over an outstanding response"
    assert signal.getsignal(signal.SIGINT) == previous_handler
    assert "accept" in ops.trace
    if during == "prepare":
        assert "discard" in ops.trace


def test_second_ctrlc_can_abort_wait_without_forcing_the_child(monkeypatch):
    from wav2sum import devwatch

    ops = Fake()
    ops.prepares.append("busy")
    ops.on_sleep = lambda: signal.raise_signal(signal.SIGINT)
    monkeypatch.setattr(devwatch, "Runtime", lambda: ops)
    previous_handler = signal.getsignal(signal.SIGINT)
    run(cloud=False)
    assert ("1", "exit") not in ops.trace
    assert ("1", "detach") in ops.trace
    assert signal.getsignal(signal.SIGINT) == previous_handler
    assert any(item[0] == "report" and "interrupted" in item[1] for item in ops.trace if isinstance(item, tuple))


def test_ctrlc_ends_owned_dev_session(monkeypatch):
    from wav2sum import devwatch

    ops = Fake()
    monkeypatch.setattr(devwatch, "Runtime", lambda: ops)

    def interrupt_once():
        ops.on_sleep = None
        signal.raise_signal(signal.SIGINT)

    ops.on_sleep = interrupt_once
    run(cloud=False)
    assert ("launch", False) in ops.trace
    assert ("1", "exit") in ops.trace
    assert ("1", "detach") in ops.trace
    assert ops.trace[-1] == "close"


def test_unknown_installed_identity_builds_initial_snapshot():
    ops = Fake()
    ops.installed = None
    ops, watch = started(ops)
    watch.poll()
    assert watch.applied == "v1"
    assert ("build", "v1") in ops.trace


def test_verify_failure_never_launches():
    ops = Fake()

    def fail():
        raise OSError("unverified")

    ops.verify_target = fail
    with pytest.raises(WatchError, match="boot"):
        Watcher(ops, cloud=False).start()
    assert ops.launches == 0


def test_busy_source_change_never_exits_old_child():
    ops, watch = started()
    ops.source = "v2"

    def changed_busy():
        ops.source = "v3"
        return "busy"

    ops.prepares.append(changed_busy)
    watch.poll()
    assert ("1", "exit") not in ops.trace
    watch.poll()
    assert watch.applied == "v3"


def test_shutdown_waits_for_busy_work_and_never_rebuilds():
    ops, watch = started()
    ops.prepares.extend(["busy", "busy", "ready"])
    watch.shutdown()
    assert ops.trace.count(("1", "prepare")) == 3
    assert ops.trace.index(("1", "wait")) > ops.trace.index(("1", "exit"))
    assert not any(isinstance(item, tuple) and item[0] in ("swap", "build") for item in ops.trace)
    assert watch.stopped
    assert "accept" in ops.trace


def test_shutdown_uncertain_exit_keeps_recovery_artifacts():
    ops, watch = started()
    ops.exit_confirmed = False
    with pytest.raises(WatchError, match="exit"):
        watch.shutdown()
    assert "accept" not in ops.trace
    assert "discard" not in ops.trace
    assert watch.stopped


def test_success_discards_backup_only_after_new_child_protocol_ack():
    ops, watch = started()
    ops.source = "v2"
    watch.poll()
    assert ops.trace.index("accept") > ops.trace.index(("2", "protocol"))


def test_prepare_interruption_attempts_resume_not_exit():
    ops, watch = started()
    ops.source = "v2"

    def interrupt():
        raise KeyboardInterrupt

    ops.prepares.append(interrupt)
    with pytest.raises(KeyboardInterrupt):
        watch.poll()
    assert ("1", "resume") in ops.trace
    assert ("1", "exit") not in ops.trace
