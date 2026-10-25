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


def test_history_is_capped_and_missing_file_is_empty(tmp_path):
    path = tmp_path / "dictations.jsonl"
    assert load_dictations(path) == []
    assert not delete_dictation({"text": "x"}, path)
    for i in range(5):
        append_dictation({"text": str(i)}, path)
    assert [e["text"] for e in load_dictations(path, limit=3)] == ["4", "3", "2"]


def test_deleting_one_of_two_identical_dictations_keeps_the_other(tmp_path):
    path = tmp_path / "dictations.jsonl"
    entry = {"time": "10:00", "text": "Да"}
    append_dictation(entry, path)
    append_dictation(entry, path)
    assert delete_dictation(entry, path)
    assert load_dictations(path) == [entry]


def test_deleting_a_call_whose_audio_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "CAN_TRASH", False)
    folder = tmp_path / "output" / "call-1"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"audio": str(tmp_path / "recordings" / "call-1.wav")}))
    delete_call(folder, tmp_path / "recordings")
    assert not folder.exists()
