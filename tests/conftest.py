import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from wav2sum import client, credentials


@pytest.fixture(autouse=True)
def no_user_keychain(monkeypatch):
    class EmptyKeychain:
        def __init__(self, **kwargs):
            pass

        def get(self, name):
            return None

        def set(self, name, value):
            pytest.fail("A test must explicitly isolate Keychain before writing")

    monkeypatch.setattr(credentials, "Keychain", EmptyKeychain)


@pytest.fixture
def socket_path(monkeypatch):
    # macOS caps unix socket paths at 104 bytes, and pytest's tmp_path is often longer than that.
    with tempfile.TemporaryDirectory(prefix="w2s", dir="/tmp") as folder:
        path = Path(folder) / "d.sock"
        monkeypatch.setattr(client, "SOCKET_PATH", path)
        yield path


@pytest.fixture(scope="session")
def devwatch_binary(tmp_path_factory):
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("Swift on macOS required")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("devwatch") / "handoff"
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
    return binary
