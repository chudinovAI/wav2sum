import hashlib
import io
import zipfile

import pytest

from wav2sum import local_models


def _archive(name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, b"worker")
    return buffer.getvalue()


def test_speech_release_is_checksum_checked_and_installed(monkeypatch, tmp_path):
    data = _archive("speech")
    monkeypatch.setattr(local_models, "model_root", lambda: tmp_path)
    monkeypatch.setattr(local_models.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(local_models.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(local_models.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    monkeypatch.setattr(local_models, "SPEECH_SHA256", hashlib.sha256(data).hexdigest())
    local_models._prepare_speech()
    assert (tmp_path / "speech-0.9.4" / "speech").read_bytes() == b"worker"


def test_speech_release_rejects_bad_hash_and_archive_traversal(monkeypatch, tmp_path):
    data = _archive("../outside")
    monkeypatch.setattr(local_models, "model_root", lambda: tmp_path)
    monkeypatch.setattr(local_models.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(local_models.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(local_models.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    with pytest.raises(RuntimeError, match="checksum"):
        local_models._prepare_speech()
    monkeypatch.setattr(local_models, "SPEECH_SHA256", hashlib.sha256(data).hexdigest())
    with pytest.raises(RuntimeError, match="Unsafe"):
        local_models._prepare_speech()
    assert not (tmp_path.parent / "outside").exists()
