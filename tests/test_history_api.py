import asyncio
import json
from types import SimpleNamespace

import pytest
from test_daemon import _daemon, _serving

from wav2sum import daemon
from wav2sum.client import DaemonClient, DaemonError


def test_history_rpc_reads_custom_paths_jobs_and_duplicates_without_writes(tmp_path, monkeypatch, socket_path):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.recordings_dir = tmp_path / "custom recordings"
        d.cfg.output_dir = tmp_path / "custom output"
        d.cfg.recordings_dir.mkdir()
        raw = d.cfg.recordings_dir / "call-raw.wav"
        raw.write_bytes(b"synthetic raw")
        active = d.cfg.recordings_dir / "call-active.wav"
        active.write_bytes(b"synthetic active")
        processed_audio = d.cfg.recordings_dir / "call-done.wav"
        processed_audio.write_bytes(b"synthetic processed")
        folder = d.cfg.output_dir / "call-done"
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text(
            json.dumps({"audio": str(processed_audio), "duration_sec": 42, "speakers": ["A"]})
        )
        (folder / "transcript.txt").write_text("synthetic transcript")
        (folder / "summary.md").write_text("synthetic summary")
        entry = {"time": "2026-11-10T12:00:00+00:00", "raw": "raw example", "text": "Edited example", "style": "chat"}
        daemon.HISTORY_PATH.write_text((json.dumps(entry) + "\n") * 2)
        d.jobs = [
            daemon.Job(1, str(raw), state="failed", error="old failure"),
            daemon.Job(2, str(raw), state="running", stage="Transcribing"),
        ]
        d.recorder = SimpleNamespace(path=active, seconds=1.0)
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        async with _serving(d, socket_path) as client:
            first = await client.request("history")
            second = await client.request("history")
            status = await client.request("status")
        calls = {row["path"]: row for row in first["calls"]}
        assert calls[str(raw)]["state"] == "running" and calls[str(raw)]["stage"] == "Transcribing"
        assert calls[str(active)]["state"] == "recording"
        assert calls[str(folder)]["state"] == "done"
        assert calls[str(folder)]["open_path"] == str(folder / "summary.md")
        assert str(processed_audio) not in calls
        assert first["output_dir"] == status["output_dir"] == str(d.cfg.output_dir)
        assert first["recordings_dir"] == status["recordings_dir"] == str(d.cfg.recordings_dir)
        assert [row["text"] for row in first["dictations"]] == ["Edited example", "Edited example"]
        assert len({row["id"] for row in first["dictations"]}) == 2
        assert first["dictations"] == second["dictations"]
        assert {path: path.read_bytes() for path in before} == before
        assert d.queue.empty()

    asyncio.run(scenario())


def test_history_rpc_reports_read_failure_instead_of_empty_success(tmp_path, monkeypatch, socket_path):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        daemon.HISTORY_PATH.write_text("not json\n")
        async with _serving(d, socket_path) as client:
            with pytest.raises(DaemonError):
                await client.request("history")
        assert daemon.HISTORY_PATH.read_text() == "not json\n"

    asyncio.run(scenario())


def test_history_in_cloud_does_not_require_provider_credentials(tmp_path, monkeypatch, socket_path):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.cloud = True
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        async with _serving(d, socket_path):
            matching = await DaemonClient.connect(cloud=True)
            try:
                reply = await matching.request("history")
                assert reply["calls"] == [] and reply["dictations"] == []
            finally:
                await matching.close()
        assert d.queue.empty()

    asyncio.run(scenario())


def test_history_respects_existing_dictation_limit_and_updates_after_removal(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        entries = [{"time": str(i), "raw": f"raw {i}", "text": f"text {i}"} for i in range(502)]
        daemon.HISTORY_PATH.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        first = await d._handle({"cmd": "history"}, None)
        assert [row["text"] for row in first["dictations"]] == [f"text {i}" for i in range(501, 1, -1)]
        retained_id = first["dictations"][1]["id"]
        daemon.HISTORY_PATH.write_text("".join(json.dumps(entry) + "\n" for entry in entries[:-1]))
        after = await d._handle({"cmd": "history"}, None)
        assert after["dictations"][0]["id"] == retained_id
        assert all(row["id"] != first["dictations"][0]["id"] for row in after["dictations"])

    asyncio.run(scenario())
