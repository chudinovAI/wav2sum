import json
import os
import stat

import pytest

from wav2sum import sharing


def _write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _sources(tmp_path):
    history, edits = tmp_path / "dictations.jsonl", tmp_path / "edits.jsonl"
    _write(
        history,
        [
            {
                "raw": "привет как дела",
                "text": "Привет, как дела?",
                "language": "ru",
                "style": "chat",
                "time": "private time",
                "app": "private app",
                "title": "private title",
                "audio": "/private/audio",
            },
            {"raw": "hello world", "text": "Hello world.", "language": "en", "style": "email"},
        ],
    )
    _write(
        edits,
        [
            {
                "raw": "залей на гитхаб",
                "text": "Залей на гитхаб",
                "edited": "Залей на GitHub",
                "language": "ru",
                "style": "chat",
                "time": "private time",
            },
        ],
    )
    return history, edits


def _list(history, edits):
    return sharing.list_examples(history_path=history, edits_path=edits)


def _selection(example, target=None):
    return {"id": example["id"], "target": example["target"] if target is None else target, "confirmed": True}


def _export(path, selections, history, edits, consent=True):
    return sharing.export_examples(path, selections, consent=consent, history_path=history, edits_path=edits)


def test_review_fields_provenance_and_stable_ids(tmp_path):
    history, edits = _sources(tmp_path)
    originals = history.read_bytes(), edits.read_bytes()
    examples = _list(history, edits)
    assert len(examples) == 3
    assert all(
        set(row) == {"id", "raw", "text", "target", "language", "style", "source", "confirmed"} for row in examples
    )
    edit = next(row for row in examples if row["source"] == "edit")
    assert (edit["text"], edit["target"], edit["confirmed"]) == ("Залей на гитхаб", "Залей на GitHub", True)
    assert all(not row["confirmed"] for row in examples if row["source"] == "dictation")
    assert len({row["id"] for row in examples}) == 3
    assert all("private" not in row["id"] and row["raw"] not in row["id"] for row in examples)
    assert (history.read_bytes(), edits.read_bytes()) == originals

    rows = [json.loads(line) for line in history.read_text().splitlines()]
    _write(history, [rows[1], {"raw": "new", "text": "new", "language": "en", "style": "chat"}])
    after = _list(history, edits)
    assert (
        next(row for row in after if row["raw"] == "hello world")["id"]
        == next(row for row in examples if row["raw"] == "hello world")["id"]
    )
    assert next(row for row in after if row["source"] == "edit")["id"] == edit["id"]


def test_export_selected_and_all_only_allowlisted_unicode_with_consent(tmp_path):
    history, edits = _sources(tmp_path)
    original = history.read_bytes(), edits.read_bytes()
    examples = _list(history, edits)
    target = "Исправленный текст"
    selected = [next(row for row in examples if row["source"] == "dictation")]
    output = tmp_path / "one.jsonl"
    assert _export(output, [_selection(selected[0], target)], history, edits) == 1
    [row] = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert row == {
        "schema_version": 1,
        "raw": selected[0]["raw"],
        "target": target,
        "language": selected[0]["language"],
        "style": selected[0]["style"],
        "consent": "training",
    }
    assert stat.S_IMODE(output.stat().st_mode) & 0o077 == 0
    assert "private" not in output.read_text()
    all_path = tmp_path / "all.jsonl"
    assert _export(all_path, [_selection(row) for row in examples], history, edits) == len(examples)
    exported = [json.loads(line) for line in all_path.read_text(encoding="utf-8").splitlines()]
    assert [(row["raw"], row["target"]) for row in exported] == [(row["raw"], row["target"]) for row in examples]
    assert (history.read_bytes(), edits.read_bytes()) == original


@pytest.mark.parametrize(
    "selections,consent",
    [
        ([], True),
        (None, True),
        ("invalid", True),
        ([{}], True),
        ([{"id": "missing", "target": "ok", "confirmed": True}], True),
        ([{"id": "x", "target": "ok", "confirmed": True}], False),
        ([{"id": "x", "target": "ok", "confirmed": True}], 1),
    ],
)
def test_invalid_or_missing_consent_and_selections_leave_no_output(tmp_path, selections, consent):
    history, edits = _sources(tmp_path)
    output = tmp_path / "out.jsonl"
    with pytest.raises((ValueError, TypeError)):
        _export(output, selections, history, edits, consent)
    assert not output.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"confirmed": False},
        {"confirmed": 1},
        {"target": ""},
        {"target": "  "},
        {"target": None},
        {"id": 1},
        {"unknown": "value"},
    ],
)
def test_invalid_selection_fields_are_rejected(tmp_path, change):
    history, edits = _sources(tmp_path)
    selection = _selection(_list(history, edits)[0]) | change
    out = tmp_path / "out.jsonl"
    with pytest.raises((ValueError, TypeError)):
        _export(out, [selection], history, edits)
    assert not out.exists()


def test_duplicate_selection_and_changed_or_deleted_source_fail(tmp_path):
    history, edits = _sources(tmp_path)
    example = next(row for row in _list(history, edits) if row["source"] == "dictation")
    selection = _selection(example)
    out = tmp_path / "out.jsonl"
    with pytest.raises(ValueError):
        _export(out, [selection, selection], history, edits)
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    rows[0]["raw"] = "changed unreviewed content"
    _write(history, rows)
    with pytest.raises(ValueError):
        _export(out, [selection], history, edits)
    _write(history, rows[1:])
    with pytest.raises(ValueError):
        _export(out, [selection], history, edits)
    assert not out.exists()


@pytest.mark.parametrize(
    "bad",
    [b'{"raw":\n', b'{"raw":"\xff"}\n', b'{"raw":NaN}\n', b"null\n", b'{"raw":"x","raw":"y"}\n', b'{"raw":"x"}\n'],
)
def test_malformed_source_fails_closed_without_disclosing_text(tmp_path, bad):
    history, edits = _sources(tmp_path)
    selection = _selection(_list(history, edits)[0])
    original = history.read_bytes() + bad
    history.write_bytes(original)
    out = tmp_path / "out.jsonl"
    with pytest.raises(ValueError) as exc:
        _list(history, edits)
    assert "привет" not in str(exc.value)
    with pytest.raises(ValueError):
        _export(out, [selection], history, edits)
    assert not out.exists()
    assert history.read_bytes() == original


def test_existing_output_untouched_and_publication_failure_cleans_temp(tmp_path, monkeypatch):
    history, edits = _sources(tmp_path)
    selection = _selection(_list(history, edits)[0])
    out = tmp_path / "out.jsonl"
    out.write_bytes(b"existing data")
    with pytest.raises(FileExistsError):
        _export(out, [selection], history, edits)
    assert out.read_bytes() == b"existing data"
    out.unlink()
    before = set(tmp_path.iterdir())

    def fail_link(*args, **kwargs):
        raise OSError("publication failed")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError):
        _export(out, [selection], history, edits)
    assert set(tmp_path.iterdir()) == before
    assert not out.exists()


def test_missing_history_is_empty_and_duplicate_records_collapse(tmp_path):
    history, edits = tmp_path / "history.jsonl", tmp_path / "edits.jsonl"
    assert _list(history, edits) == []
    row = {"raw": "a", "text": "A", "language": "en", "style": "chat"}
    _write(history, [row, row])
    assert len(_list(history, edits)) == 1


def test_failed_sync_leaves_no_partial_export_or_source_changes(tmp_path, monkeypatch):
    history, edits = _sources(tmp_path)
    originals = history.read_bytes(), edits.read_bytes()
    selection = _selection(_list(history, edits)[0])
    before = set(tmp_path.iterdir())
    out = tmp_path / "out.jsonl"

    def fail_sync(_fd):
        raise OSError("sync failed")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError):
        _export(out, [selection], history, edits)
    assert not out.exists()
    assert set(tmp_path.iterdir()) == before
    assert (history.read_bytes(), edits.read_bytes()) == originals


@pytest.mark.parametrize("linked", ["symlink", "hardlink"])
def test_review_refuses_linked_lock_without_truncating_source(tmp_path, linked):
    history, edits = _sources(tmp_path)
    original = history.read_bytes(), edits.read_bytes()
    lock = history.with_suffix(".lock")
    if linked == "symlink":
        lock.symlink_to(history)
    else:
        lock.hardlink_to(history)
    with pytest.raises(OSError):
        _list(history, edits)
    assert (history.read_bytes(), edits.read_bytes()) == original
