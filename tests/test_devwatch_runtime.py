"""Runtime tests use owned temporary bundles and fake OS/signing boundaries."""

import contextlib
import io
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from wav2sum import devwatch_runtime as runtime
from wav2sum.devwatch import MARKER, LaunchError, WatchError


@pytest.fixture
def harness(tmp_path, monkeypatch):
    target = tmp_path / "Wav2Sum.app"
    binary = target / "Contents/MacOS/Wav2Sum"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(MARKER.encode())
    (target / "Contents/Info.plist").write_bytes(b"synthetic plist")
    resources = target / "Contents/Resources"
    resources.mkdir()
    (resources / "Wav2SumSourceDigest").write_bytes(b"123456abcdef")
    calls = []
    state = {"process": 1, "stdout": b"", "stderr": b"", "signature": True, "snapshot": {"main.swift": b"one"}}

    @contextlib.contextmanager
    def lock(path, wait=True):
        calls.append(("lock", path, wait))
        yield True
        calls.append(("unlock",))

    def run(argv, **kwargs):
        calls.append(("run", argv))
        assert argv == ["pgrep", "-x", "Wav2Sum"]
        return SimpleNamespace(returncode=state["process"], stdout=state["stdout"], stderr=state["stderr"])

    def verify(bundle, serve):
        calls.append(("verify", bundle))
        if not state["signature"]:
            raise runtime.app_build.BuildError("bad signature")
        return ("leaf", "requirement")

    def compile_app(snapshot, destination, plist, identity, *, serve):
        calls.append(("build", snapshot.copy(), destination, plist, identity))
        shutil.copytree(target, destination)
        (destination / "Contents/MacOS/Wav2Sum").write_bytes(MARKER.encode() + b" compiled")
        return destination

    def exchange(first, second, swap):
        calls.append(("swap", first, second))
        assert swap
        temporary = tmp_path / "exchange-temp"
        first.rename(temporary)
        second.rename(first)
        temporary.rename(second)

    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "exclusive", lock)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime.app_build, "verify_dev_app", verify)
    monkeypatch.setattr(runtime.app_build, "source_snapshot", lambda _: state["snapshot"].copy())
    monkeypatch.setattr(runtime.app_build, "source_digest", lambda *_: "123456abcdef")
    monkeypatch.setattr(runtime.app_build, "build_app", compile_app)
    monkeypatch.setattr(runtime.app_build, "_atomic_publish", exchange)
    monkeypatch.setattr(runtime, "APP_PATH", target)
    monkeypatch.setattr(runtime, "BUILD_DIR", tmp_path / "cache")
    return runtime.Runtime(), state, calls, binary, tmp_path


def test_boot_verifies_common_signer_and_marker_before_any_launch(harness, monkeypatch):
    ops, state, calls, binary, _ = harness
    ops.verify_target()
    assert calls[0][0] == "lock" and calls[0][2] is False
    assert ops.installed_snapshot() == state["snapshot"]
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("unverified launch"))
    state["signature"] = False
    with pytest.raises(LaunchError) as exc:
        ops.launch(cloud=False)
    assert not exc.value.uncertain
    state["signature"] = True
    binary.write_bytes(b"no marker")
    with pytest.raises(LaunchError):
        ops.launch(cloud=False)
    ops.close()
    assert calls[-1] == ("unlock",)


@pytest.mark.parametrize("code,stdout,stderr", [(0, b"123", b""), (2, b"", b""), (1, b"x", b""), (1, b"", b"x")])
def test_process_check_is_conservative(harness, code, stdout, stderr):
    ops, state, _, _, _ = harness
    state.update(process=code, stdout=stdout, stderr=stderr)
    with pytest.raises(WatchError):
        ops.verify_target()
    ops.close()


def test_process_check_failure_does_not_launch(harness, monkeypatch):
    ops, _, _, _, _ = harness
    ops.verify_target()
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(WatchError):
        ops.launch(cloud=False)
    ops.close()


def test_platform_and_lock_refusals(harness, monkeypatch):
    ops, _, calls, _, _ = harness
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    with pytest.raises(WatchError):
        ops.verify_target()
    assert calls == []
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "exclusive", lambda *a, **k: contextlib.nullcontext(False))
    with pytest.raises(WatchError):
        ops.verify_target()


def test_installed_digest_detects_changed_snapshot(harness, monkeypatch):
    ops, _, _, _, _ = harness
    ops.verify_target()
    monkeypatch.setattr(runtime.app_build, "source_digest", lambda *_: "000000000000")
    assert ops.installed_snapshot() is None
    ops.close()


def test_stage_uses_captured_bytes_even_if_editor_changes(harness, monkeypatch):
    ops, state, calls, _, _ = harness
    ops.verify_target()
    snapshot = ops.snapshot()
    compile_app = runtime.app_build.build_app

    def edited(*args, **kwargs):
        state["snapshot"] = {"main.swift": b"newer editor save"}
        return compile_app(*args, **kwargs)

    monkeypatch.setattr(runtime.app_build, "build_app", edited)
    stage = ops.build(snapshot)
    assert stage.is_dir()
    assert next(call for call in calls if call[0] == "build")[1] == snapshot
    ops.discard()
    assert not stage.parent.exists()
    ops.close()


def test_failed_retries_reuse_only_one_owned_workspace(harness, monkeypatch):
    ops, _, _, _, root = harness
    preexisting = root / ".wav2sum-watch-existing"
    preexisting.mkdir()
    (preexisting / "keep").write_text("not owned by this session")
    ops.verify_target()
    monkeypatch.setattr(
        runtime.app_build, "build_app", lambda *a, **k: (_ for _ in ()).throw(OSError("compiler detail"))
    )
    for _ in range(3):
        with pytest.raises(WatchError) as exc:
            ops.build(ops.snapshot())
        assert "compiler detail" not in str(exc.value)
        assert len(list(root.glob(".wav2sum-watch-*"))) <= 2
    assert (preexisting / "keep").exists()
    ops.accept()
    assert list(root.glob(".wav2sum-watch-*")) == [preexisting]
    ops.close()


def test_cleanup_never_follows_a_replaced_owned_workspace(harness):
    ops, _, _, _, root = harness
    ops.verify_target()
    stage = ops.build(ops.snapshot())
    workspace = stage.parent
    assert workspace.resolve().parent == root.resolve()
    retained = root / "retained-workspace"
    workspace.rename(retained)
    workspace.symlink_to(retained, target_is_directory=True)
    with pytest.raises(WatchError, match="substituted"):
        ops.accept()
    assert (retained / "Wav2Sum.app").exists()
    ops.close()


def test_success_keeps_old_bundle_until_acceptance(harness):
    ops, _, _, original, _ = harness
    ops.verify_target()
    old_bytes = original.read_bytes()
    stage = ops.build(ops.snapshot())
    ops.swap(stage)
    assert (stage / "Contents/MacOS/Wav2Sum").read_bytes() == old_bytes
    with pytest.raises(WatchError):
        ops.discard()
    ops.accept()
    assert not stage.parent.exists()
    assert original.read_bytes().endswith(b" compiled")
    ops.close()


def test_rollback_restores_old_without_deleting_uncertain_artifacts(harness):
    ops, _, _, original, _ = harness
    ops.verify_target()
    old_bytes = original.read_bytes()
    stage = ops.build(ops.snapshot())
    ops.swap(stage)
    ops.rollback()
    assert original.read_bytes() == old_bytes
    ops.close()
    assert stage.exists(), "close alone must preserve a recovery candidate"


def test_failed_swap_does_not_allow_reverse_exchange(harness, monkeypatch):
    ops, _, calls, _, _ = harness
    ops.verify_target()
    stage = ops.build(ops.snapshot())
    monkeypatch.setattr(runtime.app_build, "_atomic_publish", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(WatchError):
        ops.swap(stage)
    with pytest.raises(WatchError):
        ops.rollback()
    ops.close()
    assert stage.exists()
    assert not any(call[0] == "swap" for call in calls)


@pytest.mark.parametrize("change", ["stage-dir", "target-dir", "stage-bytes", "target-bytes", "ancestor", "signature"])
def test_swap_refuses_changed_or_substituted_bundles(harness, change):
    ops, state, calls, target_binary, root = harness
    ops.verify_target()
    stage = ops.build(ops.snapshot())
    if change in ("stage-dir", "target-dir"):
        path = stage if change == "stage-dir" else ops.target
        previous = path.with_name("substituted")
        path.rename(previous)
        shutil.copytree(previous, path)
    elif change in ("stage-bytes", "target-bytes"):
        path = stage / "Contents/MacOS/Wav2Sum" if change == "stage-bytes" else target_binary
        path.write_bytes(MARKER.encode() + b" changed")
    elif change == "ancestor":
        previous = root / "previous-parent"
        stage.parent.rename(previous)
        stage.parent.symlink_to(previous, target_is_directory=True)
    else:
        state["signature"] = False
    with pytest.raises(WatchError):
        ops.swap(stage)
    assert not any(call[0] == "swap" for call in calls)
    ops.close()


def test_rollback_refuses_modified_backup(harness):
    ops, _, calls, _, _ = harness
    ops.verify_target()
    stage = ops.build(ops.snapshot())
    ops.swap(stage)
    (stage / "Contents/MacOS/Wav2Sum").write_bytes(MARKER.encode() + b" changed")
    with pytest.raises(WatchError):
        ops.rollback()
    assert len([call for call in calls if call[0] == "swap"]) == 1
    ops.close()


def test_symlink_ancestor_rejected_before_verification(harness):
    ops, _, calls, _, root = harness
    link = root / "linked"
    link.symlink_to(root, target_is_directory=True)
    ops.target = link / ops.target.name
    with pytest.raises(WatchError):
        ops.verify_target()
    assert not any(call[0] == "verify" for call in calls)
    ops.close()


def test_mutation_during_verification_is_detected(harness, monkeypatch):
    ops, _, calls, target_binary, _ = harness
    ops.verify_target()
    stage = ops.build(ops.snapshot())
    verify = ops._verify_bundle

    def changing(bundle):
        signature = verify(bundle)
        if bundle == stage:
            target_binary.write_bytes(MARKER.encode() + b" changed")
        return signature

    monkeypatch.setattr(ops, "_verify_bundle", changing)
    with pytest.raises(WatchError):
        ops.swap(stage)
    assert not any(call[0] == "swap" for call in calls)
    ops.close()


class FakePipe(io.BytesIO):
    def fileno(self):
        return 42


class FakeProcess:
    def __init__(self):
        self.stdin = FakePipe()
        self.stdout = FakePipe()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def test_exact_owned_child_and_pipe_protocol(harness, monkeypatch):
    ops, _, _, _, _ = harness
    ops.verify_target()
    process = FakeProcess()
    launches = []
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: (launches.append((a, k)), process)[1])
    monkeypatch.setattr(runtime.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(runtime.select, "select", lambda r, w, x, timeout: (r if not w else [], w, []))
    monkeypatch.setattr(runtime.os, "read", lambda *a: b"ready\n")
    writes = []
    monkeypatch.setattr(runtime.os, "write", lambda fd, data: (writes.append(data), len(data))[1])
    child = ops.launch(cloud=True)
    args, kwargs = launches[0]
    assert args[0] == [str(ops.target / "Contents/MacOS/Wav2Sum"), "--cloud"]
    assert kwargs["env"]["WAV2SUM_DEV_WATCH"] == "1" and kwargs["start_new_session"]
    assert child.request("prepare", 1) == "ready"
    child.send_exit()
    assert writes == [b"prepare\n", b"exit\n"]
    assert child.alive() and not child.wait_exit(0.01)
    process.returncode = 0
    assert child.wait_exit(0.01)
    child.detach()
    assert process.stdin.closed and process.stdout.closed
    ops.close()


@pytest.mark.parametrize("output", [b"ready\nextra\n", b"", b"x" * 257, b"\xff\n"])
def test_pipe_refuses_malformed_or_missing_response(monkeypatch, output):
    monkeypatch.setattr(runtime.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(runtime.select, "select", lambda r, w, x, timeout: (r, w, []))
    monkeypatch.setattr(runtime.os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(runtime.os, "read", lambda *a: output)
    with pytest.raises(WatchError):
        runtime.RuntimeChild(FakeProcess()).request("prepare", 1)


def test_pipe_rejects_unsolicited_later_response(monkeypatch):
    monkeypatch.setattr(runtime.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(runtime.select, "select", lambda r, w, x, timeout: (r, w, []))
    monkeypatch.setattr(runtime.os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(runtime.os, "read", lambda *a: b"ready\n")
    child = runtime.RuntimeChild(FakeProcess())
    assert child.request("prepare", 1) == "ready"
    with pytest.raises(WatchError):
        child.request("resume", 1)


@pytest.mark.parametrize("failure", ["spawn", "pipes"])
def test_uncertain_launch_is_not_retried(harness, monkeypatch, failure):
    ops, _, _, _, _ = harness
    ops.verify_target()
    launches = []

    def spawn(*a, **k):
        launches.append(1)
        if failure == "spawn":
            raise OSError("unknown spawn result")
        return FakeProcess()

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "RuntimeChild", lambda p: (_ for _ in ()).throw(OSError("pipes")))
    with pytest.raises((OSError, LaunchError)):
        ops.launch(cloud=False)
    with pytest.raises(WatchError):
        ops.launch(cloud=False)
    assert launches == [1]
    ops.close()
