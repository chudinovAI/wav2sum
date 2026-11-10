"""Isolated Swift/Python pipe integration; never launches the app or daemon."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from wav2sum.devwatch import MARKER
from wav2sum.devwatch_runtime import RuntimeChild


def test_real_swift_handoff_over_child_pipes(devwatch_binary):
    subprocess.run([str(devwatch_binary)], check=True, capture_output=True, timeout=5)
    process = subprocess.Popen(
        [str(devwatch_binary), "--pipe-child"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True
    )
    child = RuntimeChild(process)
    try:
        assert child.request("protocol", 5) == MARKER
        assert child.request("prepare", 5) == "ready"
        assert child.request("resume", 5) == "resumed"
        assert child.request("prepare", 5) == "ready"
        child.send_exit()
        assert child.wait_exit(5)
        assert process.returncode == 0
    finally:
        child.detach()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    process = subprocess.Popen(
        [str(devwatch_binary), "--eof-child"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True
    )
    child = RuntimeChild(process)
    try:
        assert child.request("prepare", 5) == "ready"
        child.stdin.close()
        assert child.wait_exit(5)
        assert child.stdout.read() == b"restored\n"
        assert process.returncode == 0
    finally:
        child.detach()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None, reason="Swift on macOS required")
def test_real_controller_handoff_with_synthetic_io(tmp_path):
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "dictation-handoff"
    subprocess.run(
        [
            "swiftc",
            str(root / "wav2sum/native/app/Dictation.swift"),
            str(root / "tests/native_dictation_handoff.swift"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
