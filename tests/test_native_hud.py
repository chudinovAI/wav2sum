"""Run the real AppKit HUD without the microphone, hotkey or daemon."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None, reason="Swift on macOS required")
def test_dictation_hud(tmp_path):
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "hud"
    subprocess.run(
        [
            "swiftc",
            "-parse-as-library",
            str(root / "wav2sum/native/app/Daemon.swift"),
            str(root / "wav2sum/native/app/Dictation.swift"),
            str(root / "tests/native_hud.swift"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    result = subprocess.run([str(binary)], check=False, capture_output=True, text=True, timeout=10)
    if result.returncode == 77:
        pytest.skip(result.stdout.strip())
    result.check_returncode()
