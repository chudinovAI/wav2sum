import asyncio
import json

import pytest
from textual.widgets import DataTable, Markdown, Static

from wav2sum import daemon, history, tui
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
        # Separate presses: in one press Textual moves on to "x" once the process looks idle, and "x" can
        # then reach the calls panel before "3" has moved the focus, so the call gets deleted instead.
        await pilot.press("3")
        await pilot.press("x", "y")
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


def test_search_filters_calls_and_dictations(app):
    async def scenario(pilot):
        await pilot.press("slash", *"РЕЛИЗ")
        assert len(_rows(app, "calls")) == 1
        assert _rows(app, "dictations") == []
        await pilot.press(*["backspace"] * 5)
        await pilot.press(*"привет")
        assert len(_rows(app, "calls")) == 1
        assert len(_rows(app, "dictations")) == 1
        await pilot.press("x", "enter")
        assert _rows(app, "calls") == _rows(app, "dictations") == []
        assert app.focused.id == "calls"
        await pilot.press("escape")
        assert len(_rows(app, "calls")) == len(_rows(app, "dictations")) == 1

    _run(app, scenario)


def test_mixed_calls_deduplicate_and_search_raw(app, tmp_path):
    raw = tmp_path / "recordings" / "call-raw.wav"
    raw.write_bytes(b"RIFF")

    async def scenario(pilot):
        rows = _rows(app, "calls")
        assert len(rows) == 2
        assert sum(row[0] == "2026-10-20_14-15-56" for row in rows) == 1
        assert next(row for row in rows if row[0] == "raw")[1] == "raw"
        await pilot.press("slash", *"raw", "enter")
        assert [row[0] for row in _rows(app, "calls")] == ["raw"]

    _run(app, scenario)


def test_process_selected_raw_creates_a_result(app, tmp_path, monkeypatch, socket_path):
    raw = tmp_path / "recordings" / "call-raw.wav"
    raw.write_bytes(b"RIFF")
    monkeypatch.setattr(daemon, "VOCABULARY_PATH", tmp_path / "vocabulary.json")
    monkeypatch.setattr(daemon, "EDITS_PATH", tmp_path / "edits.jsonl")

    def process(audio, models, out_root, **kwargs):
        folder = out_root / audio.stem
        folder.mkdir()
        (folder / "meta.json").write_text(json.dumps({"audio": str(audio)}))
        (folder / "summary.md").write_text("Synthetic summary")
        return type("Result", (), {"out_dir": folder})()

    monkeypatch.setattr(daemon, "process", process)

    async def scenario():
        d = daemon.Daemon(app.cfg)
        d.loop = asyncio.get_running_loop()
        d.ready, d.queue = asyncio.Event(), asyncio.Queue()
        d.ready.set()
        monkeypatch.setattr(d.models, "unload", lambda *_: None)
        server = await asyncio.start_unix_server(d._serve, path=str(socket_path))
        worker = asyncio.create_task(d._work())
        try:
            async with app.run_test(size=(160, 50)) as pilot:
                async with asyncio.timeout(5):
                    while app.client is None:
                        await pilot.pause()
                    table = app.query_one("#calls", DataTable)
                    table.move_cursor(row=next(i for i, row in enumerate(_rows(app, "calls")) if row[0] == "raw"))
                    await pilot.press("p")
                    while "Synthetic summary" not in app.query_one("#view", Markdown).source:
                        await pilot.pause()
                assert raw.read_bytes() == b"RIFF"
                assert (tmp_path / "output" / raw.stem / "summary.md").read_text() == "Synthetic summary"
                assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value.endswith(raw.stem)
        finally:
            worker.cancel()
            server.close()
            for writer in tuple(d.connections):
                writer.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_raw_cannot_be_deleted_or_opened(app, tmp_path, monkeypatch):
    raw = tmp_path / "recordings" / "call-raw.wav"
    raw.write_bytes(b"RIFF")
    opened, notes = [], []
    monkeypatch.setattr(tui.subprocess, "run", lambda *args, **kwargs: opened.append(args))

    async def scenario(pilot):
        app.notify = lambda message, **_: notes.append(message)
        table = app.query_one("#calls", DataTable)
        table.move_cursor(row=next(i for i, row in enumerate(_rows(app, "calls")) if row[0] == "raw"))
        await pilot.pause()
        await pilot.press("x", "o")
        assert raw.exists()
        assert not opened
        assert any("raw" in note.lower() and "delet" in note.lower() for note in notes)

    _run(app, scenario)


def test_recording_end_refreshes_calls(app, tmp_path):
    raw = tmp_path / "recordings" / "call-new.wav"

    async def scenario(pilot):
        raw.write_bytes(b"RIFF")
        app.handle({"event": "recording", "active": False, "path": str(raw)})
        assert any(row[0] == "new" and row[1] == "raw" for row in _rows(app, "calls"))
        assert raw.exists()

    _run(app, scenario)


def test_reconnect_loads_jobs_and_completion_keeps_selection(app, tmp_path, monkeypatch):
    raw = tmp_path / "recordings" / "call-new.wav"
    raw.write_bytes(b"RIFF")
    job = {"id": 9, "audio": str(raw), "state": "running", "stage": "Transcribing", "out_dir": None, "error": None}

    class Client:
        async def request(self, method, **kwargs):
            assert method == "subscribe"
            return {"state": "ready", "jobs": [job]}

        async def events(self):
            await asyncio.Event().wait()
            yield

    async def connect():
        return Client()

    monkeypatch.setattr(tui.DaemonClient, "connect", connect)

    async def scenario(pilot):
        assert _rows(app, "jobs") == [["new", "Transcribing"]]
        assert next(row for row in _rows(app, "calls") if row[0] == "new")[1] == "running"
        table = app.query_one("#calls", DataTable)
        table.move_cursor(row=next(i for i, row in enumerate(_rows(app, "calls")) if row[0] == "new"))
        await pilot.pause()
        folder = tmp_path / "output" / raw.stem
        folder.mkdir()
        (folder / "meta.json").write_text(json.dumps({"audio": str(raw)}))
        (folder / "summary.md").write_text("Finished summary")
        app.handle({"event": "job", **job, "state": "done", "out_dir": str(folder)})
        assert len([row for row in _rows(app, "calls") if row[0] == "new"]) == 1
        assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value == str(folder)
        assert "Finished summary" in app.query_one("#view", Markdown).source
        await pilot.press("4")
        app.handle({"event": "job", **job, "state": "failed", "error": "failed"})
        assert "failed" in app.query_one("#view", Markdown).source
        assert str(raw) not in app.query_one("#view", Markdown).source

    _run(app, scenario)
