import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from wav2sum import history

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def test_prune_keeps_boundary_unknown_and_raw_bytes_and_only_touches_history(tmp_path):
    path = tmp_path / "dictations.jsonl"
    expired = b'{"time":"2026-10-27T11:59:59.999999Z","text":"old"}\n'
    expired_offset = b'{"time":"2026-10-27T14:59:59+03:00"}\n'
    kept = [
        b'{ "time" : "2026-10-27T15:00:00+03:00", "text": "boundary" }\r\n',
        b'{"time":"2026-10-27T07:00:00-05:00","text":"offset boundary"}\n',
        b'{"time":"2026-11-04T00:00:00Z"}\n',
        b'{"time":"2026-10-01T00:00:00"}\n',
        b'{"time":"invalid"}\n',
        b'{"time":null}\n',
        b'{"time":123}\n',
        b'{"text":"missing"}\n',
        b"null\n",
        b"\n",
        '{"time":"2026-10-28T00:00:00Z","text":"Привет"}'.encode(),
    ]
    path.write_bytes(expired + kept[0] + expired_offset + b"".join(kept[1:]))
    path.chmod(0o640)
    neighbors = {
        tmp_path / "vocabulary.json": b'{"terms":["keep"]}',
        tmp_path / "edits.jsonl": b'{"time":"2020-01-01T00:00:00Z"}\n',
        tmp_path / "call.wav": b"audio data",
        tmp_path / "call.json": b'{"time":"2020-01-01T00:00:00Z"}',
    }
    for neighbor, content in neighbors.items():
        neighbor.write_bytes(content)

    assert history.prune_dictations(path, now=NOW) == 2
    assert path.read_bytes() == b"".join(kept)
    assert path.stat().st_mode & 0o777 == 0o640
    assert {p: p.read_bytes() for p in neighbors} == neighbors


def test_missing_empty_and_unexpired_history_are_not_rewritten(tmp_path):
    path = tmp_path / "dictations.jsonl"
    assert history.prune_dictations(path, now=NOW) == 0
    assert not path.exists()
    for content in (b"", b'{"time":"2026-10-27T12:00:00Z"}\n'):
        path.write_bytes(content)
        before = path.stat()
        assert history.prune_dictations(path, now=NOW) == 0
        after = path.stat()
        assert path.read_bytes() == content
        assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


@pytest.mark.parametrize("bad", [b'{"broken":\n', b'{"text":"\xff"}\n', b'{"time":NaN}\n'])
def test_bad_data_fails_closed_without_exposing_contents(tmp_path, bad, caplog):
    path = tmp_path / "dictations.jsonl"
    original = b'{"time":"2020-01-01T00:00:00Z","text":"private phrase"}\n' + bad
    path.write_bytes(original)
    before = path.stat()
    with pytest.raises((ValueError, UnicodeError)) as error:
        history.prune_dictations(path, now=NOW)
    assert "private phrase" not in str(error.value)
    assert "private phrase" not in caplog.text
    assert path.read_bytes() == original
    assert path.stat().st_ino == before.st_ino


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_write_or_replace_preserves_original_and_cleans_temp(tmp_path, monkeypatch, failure):
    path = tmp_path / "dictations.jsonl"
    original = b'{"time":"2020-01-01T00:00:00Z"}\n{"time":"2026-11-03T00:00:00Z"}\n'
    path.write_bytes(original)
    if failure == "replace":

        def fail_replace(*args, **kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", fail_replace)
    else:
        real_fdopen = os.fdopen

        @contextmanager
        def fail_write(fd, *args, **kwargs):
            with real_fdopen(fd, *args, **kwargs) as file:

                class Writer:
                    def __getattr__(self, name):
                        return getattr(file, name)

                    def write(self, data):
                        raise OSError("write failed")

                yield Writer()

        monkeypatch.setattr(os, "fdopen", fail_write)

    with pytest.raises(OSError):
        history.prune_dictations(path, now=NOW)
    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) <= {path, path.with_suffix(".lock")}


def test_symlink_history_is_not_replaced_or_modified(tmp_path):
    target = tmp_path / "target.jsonl"
    path = tmp_path / "dictations.jsonl"
    original = b'{"time":"2020-01-01T00:00:00Z"}\n'
    target.write_bytes(original)
    path.symlink_to(target)
    with pytest.raises(OSError):
        history.prune_dictations(path, now=NOW)
    assert path.is_symlink()
    assert target.read_bytes() == original


def test_concurrent_append_waits_for_prune_and_is_not_lost(tmp_path, monkeypatch):
    path = tmp_path / "dictations.jsonl"
    path.write_bytes(b'{"time":"2020-01-01T00:00:00Z"}\n')
    entered, release, append_started = threading.Event(), threading.Event(), threading.Event()
    real_replace = os.replace

    def paused_replace(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release replacement")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "replace", paused_replace)
    entry = {"time": "2026-11-03T12:00:00Z", "text": "concurrent"}

    def append():
        append_started.set()
        history.append_dictation(entry, path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pruning = pool.submit(history.prune_dictations, path, now=NOW)
        try:
            assert entered.wait(5)
            appending = pool.submit(append)
            assert append_started.wait(5)
            assert not appending.done()
        finally:
            release.set()
        assert pruning.result(timeout=5) == 1
        appending.result(timeout=5)
    assert [json.loads(line) for line in path.read_bytes().splitlines()] == [entry]
