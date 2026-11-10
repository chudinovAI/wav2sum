import json
import os
from pathlib import Path

import pytest

from wav2sum import catalog
from wav2sum.catalog import CallEntry, list_calls


def _result(output_dir, name, meta):
    folder = output_dir / name
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def test_lists_raw_processed_and_imported_without_modifying_sources(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    output = tmp_path / "output"
    raw = recordings / "call-z.wav"
    raw.write_bytes(b"raw audio")
    processed_audio = recordings / "call-b.wav"
    processed_audio.write_bytes(b"processed audio")
    imported = tmp_path / "imported" / raw.name
    imported.parent.mkdir()
    imported.write_bytes(b"imported audio")
    ignored = recordings / "call-ignore.mp3"
    ignored.write_bytes(b"not wav")
    linked_audio = recordings / "alias.wav"
    linked_audio.symlink_to(processed_audio)
    metadata = {"audio": str(linked_audio), "duration_sec": 42, "speakers": ["Alice"]}
    processed = _result(output, "call-b", metadata)
    imported_result = _result(output, "call-imported", {"audio": str(imported)})
    # Multiple outputs for the same recording are distinct catalog entries.
    duplicate = _result(output, "call-copy", {"audio": str(processed_audio)})
    no_audio = _result(output, "call-no-audio", {"speakers": []})
    summary = processed / "summary.md"
    summary.write_bytes(b"summary bytes")
    transcript = processed / "transcript.txt"
    transcript.write_bytes(b"\xff")  # Catalog must not decode transcript content.
    chronological = [raw, processed, imported_result, duplicate, no_audio]
    for timestamp, path in enumerate(chronological, start=1):
        os.utime(path, (timestamp, timestamp))
    snapshot = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file() and not path.is_symlink()}

    entries = list_calls(recordings, output)

    assert [entry.path for entry in entries] == list(reversed(chronological))
    by_path = {entry.path: entry for entry in entries}
    assert by_path[raw] == CallEntry(raw, raw.resolve(), None)
    assert by_path[processed] == CallEntry(processed, processed_audio.resolve(), metadata)
    assert by_path[duplicate] == CallEntry(duplicate, processed_audio.resolve(), {"audio": str(processed_audio)})
    assert by_path[imported_result] == CallEntry(imported_result, imported.resolve(), {"audio": str(imported)})
    assert by_path[no_audio] == CallEntry(no_audio, None, {"speakers": []})
    assert {path: path.read_bytes() for path in snapshot} == snapshot


def test_missing_directories_are_empty_independently(tmp_path):
    recordings = tmp_path / "recordings"
    output = tmp_path / "output"
    assert list_calls(recordings, output) == []

    recordings.mkdir()
    raw = recordings / "one.wav"
    raw.write_bytes(b"raw")
    assert list_calls(recordings, output) == [CallEntry(raw, raw.resolve(), None)]

    raw.unlink()
    recordings.rmdir()
    folder = _result(output, "one", {})
    assert list_calls(recordings, output) == [CallEntry(folder, None, {})]


def test_corrupt_metadata_is_not_hidden(tmp_path):
    folder = _result(tmp_path / "output", "broken", {})
    (folder / "meta.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        list_calls(tmp_path / "recordings", folder.parent)


@pytest.mark.parametrize("directory", ["recordings", "output"])
def test_directory_read_errors_propagate(tmp_path, monkeypatch, directory):
    recordings = tmp_path / "recordings"
    output = tmp_path / "output"
    recordings.mkdir()
    output.mkdir()
    blocked = tmp_path / directory
    original = catalog.os.scandir

    def scandir(path):
        if path == blocked:
            raise PermissionError("directory is unreadable")
        return original(path)

    monkeypatch.setattr(catalog.os, "scandir", scandir)
    with pytest.raises(PermissionError, match="directory is unreadable"):
        list_calls(recordings, output)


@pytest.mark.parametrize("directory", ["recordings", "output"])
def test_existing_non_directory_is_not_treated_as_missing(tmp_path, directory):
    blocked = tmp_path / directory
    blocked.write_bytes(b"not a directory")
    with pytest.raises(NotADirectoryError):
        list_calls(tmp_path / "recordings", tmp_path / "output")


def test_metadata_read_errors_propagate(tmp_path, monkeypatch):
    folder = _result(tmp_path / "output", "broken", {})
    meta_path = folder / "meta.json"
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == meta_path:
            raise PermissionError("metadata is unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(PermissionError, match="metadata is unreadable"):
        list_calls(tmp_path / "recordings", folder.parent)
