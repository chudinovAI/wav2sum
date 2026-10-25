import asyncio
import json

import pytest
from textual.widgets import DataTable, Markdown, Static

from wav2sum import history, tui
from wav2sum.config import Config
from wav2sum.history import append_dictation, load_dictations


@pytest.fixture
def app(tmp_path, monkeypatch, socket_path):
    dictations = tmp_path / "dictations.jsonl"
    monkeypatch.setattr(history, "CAN_TRASH", False)
    monkeypatch.setattr(tui, "LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr(tui, "load_dictations", lambda: load_dictations(dictations))
    monkeypatch.setattr(tui, "delete_dictation", lambda entry: history.delete_dictation(entry, dictations))

    append_dictation({"time": "2026-10-25T10:00:00+03:00", "text": "Привет", "raw": "привет"}, dictations)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    audio = recordings / "call-2026-10-20_14-15-56.wav"
    audio.write_bytes(b"RIFF")
    folder = tmp_path / "output" / audio.stem
    folder.mkdir(parents=True)
    meta = {"audio": str(audio), "duration_sec": 3725, "speakers": ["Андрей", "Илья"]}
    (folder / "meta.json").write_text(json.dumps(meta))
    (folder / "summary.md").write_text("**Тема** — релиз.")
    (folder / "transcript.txt").write_text("[00:00 – 00:02] Андрей: Привет\n\n[00:03 – 00:05] Илья: Здравствуй")
    return tui.Wav2SumTUI(Config(recordings_dir=recordings, output_dir=tmp_path / "output"))


def _rows(app, table_id):
    table = app.query_one(f"#{table_id}", DataTable)
    return [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]


def _run(app, scenario):
    async def go():
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await scenario(pilot)

    asyncio.run(go())


def test_lists_calls_and_dictations_and_shows_a_call(app):
    async def scenario(pilot):
        assert _rows(app, "calls") == [["2026-10-20_14-15-56", "1:02:05", "Андрей, Илья"]]
        assert _rows(app, "dictations") == [["10-25 10:00", "Привет"]]
        markdown = app.query_one("#view", Markdown).source
        assert "**Тема** — релиз." in markdown
        assert "`00:03` **Илья:** Здравствуй" in markdown
        app.refresh_status()
        assert "background process is off" in str(app.query_one("#status", Static).render())

    _run(app, scenario)


def test_deleting_a_call_asks_first(app, tmp_path):
    folder = tmp_path / "output" / "call-2026-10-20_14-15-56"

    async def scenario(pilot):
        await pilot.press("x", "n")
        assert folder.exists()
        await pilot.press("x", "y")
        await pilot.pause()
        assert not folder.exists()
        assert not (tmp_path / "recordings" / "call-2026-10-20_14-15-56.wav").exists()
        assert _rows(app, "calls") == []

    _run(app, scenario)


def test_deleting_a_dictation(app, tmp_path):
    async def scenario(pilot):
        await pilot.press("3", "x", "y")
        await pilot.pause()
        assert _rows(app, "dictations") == []
        assert load_dictations(tmp_path / "dictations.jsonl") == []

    _run(app, scenario)


def test_daemon_events_update_the_panels(app):
    async def scenario(pilot):
        notes = []
        app.notify = lambda message, **_: notes.append(message)
        app.handle({"event": "dictation", "time": "2026-10-25T11:30:00+03:00", "text": "Новая\nстрока", "raw": "x"})
        job = {"id": 7, "audio": "/x/call-2026-10-25_11-00-00.wav", "state": "running", "stage": "Summarizing"}
        app.handle({"event": "job", **job, "out_dir": None, "error": None})
        assert _rows(app, "dictations")[0] == ["10-25 11:30", "Новая строка"]
        assert _rows(app, "jobs") == [["2026-10-25_11-00-00", "Summarizing"]]

        app.handle({"event": "job", **job, "state": "failed", "out_dir": None, "error": "ffmpeg failed"})
        assert _rows(app, "jobs") == [["2026-10-25_11-00-00", "failed"]]
        assert notes == ["Processing failed: ffmpeg failed"]

    _run(app, scenario)


def test_clock_and_hotkey_labels():
    assert (tui._clock(65), tui._clock(3725)) == ("01:05", "1:02:05")
    assert tui._hotkey_label("right_option") == "right ⌥"
    assert tui._hotkey_label("f13") == "f13"


def test_dictation_shows_its_mode():
    entry = {"time": "2026-10-25T10:00:00+03:00", "text": "Hi", "raw": "привет", "style": "chat"}
    assert "· chat" in tui._dictation_markdown(entry)
    assert "· command" in tui._dictation_markdown({**entry, "command": True})
    assert "· translate" in tui._dictation_markdown({**entry, "translate": True})
