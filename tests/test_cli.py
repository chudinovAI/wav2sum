import sys
from types import SimpleNamespace

import pytest

from wav2sum import calls, cli, config, models
from wav2sum.calls import Names, Speakers
from wav2sum.config import Config
from wav2sum.conversation import Utterance


@pytest.fixture
def run(tmp_path, monkeypatch, socket_path):
    seen = {}

    def fake_process(audio, models, out_root, **kwargs):
        seen.update(audio=audio, models=models, out_root=out_root, **kwargs)
        return SimpleNamespace(
            transcript=calls.format_transcript([Utterance(0.0, 1.0, "привет", "Я")]),
            summary="**Тема** — тест." if kwargs["llm"] else None,
            out_dir=out_root / audio.stem,
        )

    monkeypatch.setattr(cli, "load_config", lambda: Config(output_dir=tmp_path / "output"))
    monkeypatch.setattr(calls, "process", fake_process)
    monkeypatch.setattr(
        models,
        "Models",
        lambda device=None, *, cloud=False, call_model="nemotron", allow_unsafe_nemotron=False: SimpleNamespace(
            device=device, cloud=cloud, call_model=call_model
        ),
    )
    monkeypatch.setattr(config, "VOCABULARY_PATH", tmp_path / "vocabulary.json")

    def main(*argv):
        monkeypatch.setattr(sys, "argv", ["wav2sum", *argv])
        cli.main()
        return seen

    return main


def test_dev_watch_is_an_explicit_app_option(monkeypatch):
    from wav2sum import devwatch

    calls = []
    monkeypatch.setattr(devwatch, "run", lambda *, cloud: calls.append(cloud))
    monkeypatch.setattr(cli, "load_config", Config)
    monkeypatch.setattr(sys, "argv", ["wav2sum", "app", "--watch", "--cloud"])
    cli.main()
    assert calls == [True]


def test_a_bare_file_is_transcribed_with_config_defaults(tmp_path, run, capsys):
    audio = tmp_path / "meeting.mp3"
    audio.write_bytes(b"")

    seen = run(str(audio))

    assert seen["audio"] == audio
    assert seen["out_root"] == tmp_path / "output"
    assert seen["layout"] is None
    assert seen["llm"] is None
    assert seen["models"].cloud is False
    assert seen["speakers"] == Speakers(2)
    assert seen["names"] == Names("Я", "Собеседник")
    assert seen["use_cache"]
    out = capsys.readouterr().out
    assert "[00:00 – 00:01] Я: привет" in out and "**Тема** — тест." not in out


@pytest.mark.parametrize(
    "argv",
    [
        ("--cloud", "transcribe", "{audio}"),
        ("transcribe", "--cloud", "{audio}"),
        ("transcribe", "{audio}", "--cloud"),
        ("--cloud", "{audio}"),
    ],
)
def test_explicit_cloud_selects_cloud_models(tmp_path, run, argv):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"")
    seen = run(*(arg.format(audio=audio) for arg in argv))
    assert seen["models"].cloud is True


def test_cloud_summary_can_be_disabled(tmp_path, run):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"")
    seen = run("--cloud", str(audio), "--no-summary")
    assert seen["models"].cloud is True
    assert seen["llm"] is None


def test_cloud_summary_model_override(tmp_path, run):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"")
    seen = run("--cloud", str(audio), "--model", "custom-model")
    assert seen["llm"].model == "custom-model"


def test_daemon_request_failure_does_not_fallback(monkeypatch, tmp_path):
    import numpy as np

    from wav2sum import audio, client

    class Connected:
        async def dictate(self, *_args, **_kwargs):
            raise client.DaemonError("mode mismatch")

        async def close(self):
            pass

    async def connect(*, cloud=False):
        return Connected()

    monkeypatch.setattr(audio, "load_audio", lambda *a, **kw: np.zeros((10, 1)))
    monkeypatch.setattr(client.DaemonClient, "connect", connect)
    monkeypatch.setattr(models, "Models", lambda *a, **kw: pytest.fail("must not fall back"))
    args = SimpleNamespace(audio=tmp_path / "synthetic.wav", app=None, title=None, command_mode=False, selection=None)
    with pytest.raises(client.DaemonError, match="mode mismatch"):
        cli._dictate(args, Config())


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


def test_cloud_keys_command_saves_exports_for_new_processes(run, monkeypatch, capsys):
    from wav2sum import credentials

    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("Key setup must not read application config"))

    values = {}

    class Store:
        def __init__(self, **kwargs):
            pass

        def set(self, name, value):
            values[name] = value

        def get(self, name):
            return values.get(name)

    monkeypatch.setattr(credentials, "Keychain", Store)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "synthetic-dg-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-oa-secret")
    run("cloud-keys")
    for name in credentials.PROVIDERS:
        monkeypatch.delenv(name)
    assert credentials.credential("DEEPGRAM_API_KEY") == "synthetic-dg-secret"
    assert credentials.credential("OPENAI_API_KEY") == "synthetic-oa-secret"
    output = capsys.readouterr()
    assert "synthetic-dg-secret" not in output.out + output.err
    assert "synthetic-oa-secret" not in output.out + output.err


def test_cancelled_key_setup_exits_without_traceback_or_writes(run, monkeypatch):
    import getpass

    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def cancel(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(getpass, "getpass", cancel)
    with pytest.raises(SystemExit, match="Cloud key setup cancelled"):
        run("cloud-keys")
