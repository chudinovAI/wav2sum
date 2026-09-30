import json

from wav2sum import history
from wav2sum.history import append_dictation, delete_call, delete_dictation, load_dictations


def test_dictations_append_load_and_delete(tmp_path):
    path = tmp_path / "dictations.jsonl"
    first, second = {"time": "10:00", "text": "Привет"}, {"time": "10:01", "text": "Пока"}
    append_dictation(first, path)
    append_dictation(second, path)
    assert load_dictations(path) == [second, first]

    assert delete_dictation(first, path)
    assert not delete_dictation(first, path)
    assert load_dictations(path) == [second]


def _call(tmp_path, audio):
    folder = tmp_path / "output" / audio.stem
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"audio": str(audio)}))
    cache = tmp_path / "output" / ".cache" / history.file_hash(audio)
    cache.mkdir(parents=True)
    return folder, cache


def test_deleting_a_call_takes_our_recording_but_not_users_file(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "CAN_TRASH", False)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    ours = recordings / "call-1.wav"
    ours.write_bytes(b"ours")
    ours.with_suffix(".json").write_text("{}")
    theirs = tmp_path / "meeting.mp3"
    theirs.write_bytes(b"theirs")

    for audio in (ours, theirs):
        folder, cache = _call(tmp_path, audio)
        delete_call(folder, recordings)
        assert not folder.exists() and not cache.exists()

    assert not ours.exists() and not ours.with_suffix(".json").exists()
    assert theirs.exists()
