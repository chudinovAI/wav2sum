import sys
from types import SimpleNamespace

import pytest

from wav2sum import calls, cli, client, config, models
from wav2sum.calls import Names, Speakers
from wav2sum.config import Config
from wav2sum.conversation import Utterance


@pytest.fixture
def run(tmp_path, monkeypatch):
    seen = {}

    def fake_process(audio, models, out_root, **kwargs):
        seen.update(audio=audio, out_root=out_root, **kwargs)
        return SimpleNamespace(
            transcript=calls.format_transcript([Utterance(0.0, 1.0, "привет", "Я")]),
            summary="**Тема** — тест.",
            out_dir=out_root / audio.stem,
        )

    monkeypatch.setattr(cli, "load_config", lambda: Config(model="cfg-model", output_dir=tmp_path / "output"))
    monkeypatch.setattr(calls, "process", fake_process)
    monkeypatch.setattr(models, "Models", lambda device=None: SimpleNamespace(device=device))
    monkeypatch.setattr(config, "VOCABULARY_PATH", tmp_path / "vocabulary.json")
    monkeypatch.setattr(client, "SOCKET_PATH", tmp_path / "missing.sock")

    def main(*argv):
        monkeypatch.setattr(sys, "argv", ["wav2sum", *argv])
        cli.main()
        return seen

    return main


def test_a_bare_file_is_transcribed_with_config_defaults(tmp_path, run, capsys):
    audio = tmp_path / "meeting.mp3"
    audio.write_bytes(b"")

    seen = run(str(audio))

    assert seen["audio"] == audio
    assert seen["out_root"] == tmp_path / "output"
    assert seen["layout"] is None
    assert seen["llm"].model == "cfg-model"
    assert seen["speakers"] == Speakers(2)
    assert seen["names"] == Names("Я", "Собеседник")
    assert seen["use_cache"]
    out = capsys.readouterr().out
    assert "[00:00 – 00:01] Я: привет" in out and "**Тема** — тест." in out


def test_flags_reach_processing(tmp_path, run):
    audio = tmp_path / "meeting.mp3"
    audio.write_bytes(b"")

    seen = run(
        "transcribe",
        str(audio),
        "--layout",
        "call",
        "--no-summary",
        "--no-cache",
        "--language",
        "en",
        "--min-speakers",
        "3",
        "--speaker",
        "0=Алексей",
        "--speaker",
        "2=Мария = PM",
        "--me",
        "Андрей",
    )

    assert seen["layout"] == "call"
    assert seen["llm"] is None
    assert not seen["use_cache"]
    assert seen["language"] == "en"
    assert seen["speakers"] == Speakers(None, 3, None)
    assert seen["names"] == Names("Андрей", "Собеседник", {0: "Алексей", 2: "Мария = PM"})


def test_missing_file_exits_with_a_message(tmp_path, run):
    with pytest.raises(SystemExit, match="File not found"):
        run(str(tmp_path / "nope.mp3"))


def test_status_without_daemon_exits_with_a_message(run):
    with pytest.raises(SystemExit, match="background process is not running"):
        run("status")


def test_level_bar_is_clamped():
    assert cli._bar(-120) == "▯" * 12
    assert cli._bar(-30) == "▮" * 6 + "▯" * 6
    assert cli._bar(10) == "▮" * 12
