import tempfile
from pathlib import Path

import pytest

from wav2sum import client


@pytest.fixture
def socket_path(monkeypatch):
    # macOS caps unix socket paths at 104 bytes, and pytest's tmp_path is often longer than that.
    with tempfile.TemporaryDirectory(prefix="w2s", dir="/tmp") as folder:
        path = Path(folder) / "d.sock"
        monkeypatch.setattr(client, "SOCKET_PATH", path)
        yield path
