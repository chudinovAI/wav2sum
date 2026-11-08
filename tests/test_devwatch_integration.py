"""Watcher integration with a synthetic Swift child and no app or daemon."""

import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from wav2sum.devwatch import Watcher
from wav2sum.devwatch_runtime import RuntimeChild


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None, reason="Swift on macOS required")
def test_watcher_switches_and_restarts_through_cli_with_owned_swift_children(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "handoff"
    subprocess.run(
        [
            "swiftc",
            "-parse-as-library",
            str(root / "wav2sum/native/app/DevWatch.swift"),
            str(root / "tests/native_devwatch.swift"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )

    class SyntheticAdapter:
        def __init__(self):
            self.source = "old"
            self.children = []
            self.swaps = []
            self.accepted = 0

        def accept(self):
            self.accepted += 1

        def discard(self):
            pytest.fail("successful handoff must not discard the candidate")

        def verify_target(self):
            assert all(child.poll() is not None for child in self.children), "previous dev session must have ended"

        def installed_snapshot(self):
            return "old"

        def snapshot(self):
            return self.source

        def build(self, snapshot):
            return snapshot

        def launch(self, *, cloud):
            process = subprocess.Popen(
                [str(binary), "--pipe-child"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True
            )
            self.children.append(process)
            return RuntimeChild(process)

        def swap(self, staged):
            self.swaps.append(staged)

        def rollback(self):
            pytest.fail("successful handoff must not roll back")

        def sleep(self, seconds):
            pass

        def report(self, message):
            assert message.startswith("Stopping dev session"), message

        def close(self):
            pass

    ops = SyntheticAdapter()
    watch = Watcher(ops, cloud=False)
    try:
        watch.start()
        ops.source = "new"
        watch.poll()
        assert ops.swaps == ["new"]
        assert len(ops.children) == 2
        assert ops.children[0].poll() == 0
        assert ops.children[1].poll() is None
        assert watch.applied == "new"
        assert ops.accepted == 1
        watch.shutdown()
        assert ops.children[1].poll() == 0
        assert ops.accepted == 2

        from wav2sum import cli, devwatch
        from wav2sum.config import Config

        def interrupt_once(seconds):
            ops.sleep = lambda _: None
            signal.raise_signal(signal.SIGINT)

        ops.sleep = interrupt_once
        monkeypatch.setattr(devwatch, "Runtime", lambda: ops)
        monkeypatch.setattr(cli, "load_config", lambda: Config(output_dir=tmp_path / "output"))
        monkeypatch.setattr(sys, "argv", ["wav2sum", "app", "--watch"])
        cli.main()
        assert len(ops.children) == 3 and ops.children[2].poll() == 0
        assert ops.accepted == 3
    finally:
        watch.close()
        for process in ops.children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
