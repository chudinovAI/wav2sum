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
