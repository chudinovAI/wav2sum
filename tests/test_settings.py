import asyncio
import json
from pathlib import Path

import pytest

from wav2sum import config as configuration
from wav2sum import settings
from wav2sum.config import Config, load_config


def choice(**changes):
    return {
        "completed": True,
        "mode": "local",
        "auto_process": False,
        "call_model": "qwen",
        "cleanup_model": "mlx-community/gemma-4-E4B-it-qat-4bit",
        "cloud_consent": False,
        "auto_cloud_consent": False,
        **changes,
    }


def test_cloud_and_automatic_upload_require_distinct_explicit_consent():
    with pytest.raises(ValueError):
        configuration.validate_settings(choice(mode="cloud"))
    with pytest.raises(ValueError):
        configuration.validate_settings(choice(mode="cloud", cloud_consent=True, auto_process=True))
    result = configuration.validate_settings(
        choice(mode="cloud", cloud_consent=True, auto_process=True, auto_cloud_consent=True)
    )
    assert result["mode"] == "cloud" and result["auto_process"]
    assert configuration.validate_settings(choice(auto_process=True))["auto_process"]


@pytest.mark.parametrize(
    "changes",
    [{"mode": "auto"}, {"auto_process": "false"}, {"call_model": "unknown"}, {"completed": False}],
)
def test_invalid_settings_never_become_completed_configuration(changes):
    with pytest.raises(ValueError):
        configuration.validate_settings(choice(**changes))


def test_preferences_do_not_persist_credentials_or_overwrite_on_failed_validation(tmp_path):
    path = tmp_path / "preferences.json"
    configuration.save_preferences(choice(), path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        configuration.save_preferences(choice(mode="cloud", openai="secret"), path)
    assert path.read_bytes() == before
    assert configuration.load_preferences(path) == choice()
    assert path.stat().st_mode & 0o777 == 0o600


def test_bad_or_symlink_preferences_fail_without_replacing_them(tmp_path):
    target = tmp_path / "original"
    target.write_text("not json")
    with pytest.raises(ValueError):
        configuration.load_preferences(target)
    path = tmp_path / "settings"
    path.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        configuration.save_preferences(choice(), path)
    assert target.read_text() == "not json"


def test_app_preferences_override_processing_not_cli_cloud_or_user_paths(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('me = "Friend"\nrecordings_dir = "~/private-calls"\n')
    prefs = tmp_path / "preferences.json"
    configuration.save_preferences(
        choice(mode="cloud", cloud_consent=True, auto_process=True, auto_cloud_consent=True), prefs
    )
    loaded = load_config(config, preferences_path=prefs)
    assert loaded.cloud is False
    assert loaded.auto_process and loaded.auto_cloud_consent
    assert loaded.call_model == "qwen"
    assert loaded.me == "Friend"
    assert loaded.recordings_dir == Path("~/private-calls").expanduser()
    assert "cloud" not in config.read_text()


def test_key_save_gathers_both_before_any_keychain_change(monkeypatch):
    saved = []

    class Keychain:
        def __init__(self, **_kwargs):
            pass

        def set(self, key, value):
            saved.append((key, value))

    monkeypatch.setattr("wav2sum.credentials.Keychain", Keychain)
    with pytest.raises(ValueError):
        settings.handle({"action": "credentials", "deepgram": "synthetic", "openai": ""})
    assert saved == []
    assert settings.handle({"action": "credentials", "deepgram": "synthetic-dg", "openai": "synthetic-ai"}) == {
        "ok": True
    }
    assert saved == [("DEEPGRAM_API_KEY", "synthetic-dg"), ("OPENAI_API_KEY", "synthetic-ai")]


def test_stdio_setup_never_echoes_unknown_input_or_exception_secrets(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"action": "secret-value"})))
    settings.main()
    reply = json.loads(capsys.readouterr().out)
    assert not reply["ok"]
    assert "secret-value" not in reply["error"]


def test_daemon_configuration_refuses_active_work_before_writing(tmp_path, monkeypatch):
    from wav2sum import daemon

    async def scenario():
        cfg = Config(recordings_dir=tmp_path)
        d = daemon.Daemon(cfg)
        d.ready = asyncio.Event()
        d.stopping = asyncio.Event()
        d.recorder = object()
        monkeypatch.setattr(configuration, "save_preferences", lambda *_a, **_k: pytest.fail("saved while recording"))
        with pytest.raises(ValueError):
            await d._configure(choice(mode="cloud", cloud_consent=True))
        assert not d.stopping.is_set() and not d.cfg.cloud
        d.recorder = None
        d.jobs = [daemon.Job(1, str(tmp_path / "call.wav"))]
        with pytest.raises(ValueError):
            await d._configure(choice(mode="cloud", cloud_consent=True))
        d.jobs = []
        d.streams = {object(): object()}
        with pytest.raises(ValueError):
            await d._configure(choice(mode="cloud", cloud_consent=True))

    asyncio.run(scenario())


def test_daemon_switch_persists_then_stops_idle_process_without_mutating_its_mode(tmp_path, monkeypatch):
    from wav2sum import daemon

    async def scenario():
        d = daemon.Daemon(Config(recordings_dir=tmp_path))
        d.ready = asyncio.Event()
        d.stopping = asyncio.Event()
        path = tmp_path / "prefs.json"
        monkeypatch.setattr(configuration, "PREFERENCES_PATH", path)
        result = await d._configure(choice(mode="cloud", cloud_consent=True))
        assert result["restart"] and d.stopping.is_set()
        assert configuration.load_preferences(path)["mode"] == "cloud"
        assert d.cfg.cloud is False

    asyncio.run(scenario())


def test_failed_warmup_can_restart_after_explicit_setup_without_changing_mode(tmp_path, monkeypatch):
    from wav2sum import daemon

    async def scenario():
        d = daemon.Daemon(Config(call_model="qwen"))
        d.stopping = asyncio.Event()
        d.warm_up_error = "models unavailable"
        monkeypatch.setattr(configuration, "PREFERENCES_PATH", tmp_path / "prefs.json")
        result = await d._configure(choice())
        assert result["restart"] and d.stopping.is_set()
        assert not d.cfg.cloud

    asyncio.run(scenario())


def test_settings_do_not_interrupt_initial_model_loading(tmp_path, monkeypatch):
    from wav2sum import daemon

    async def scenario():
        d = daemon.Daemon(Config(call_model="qwen"))
        d.stopping = asyncio.Event()
        d.loading_models = True
        monkeypatch.setattr(configuration, "save_preferences", lambda *_args: pytest.fail("saved during load"))
        with pytest.raises(ValueError):
            await d._configure(choice())
        assert not d.stopping.is_set()

    asyncio.run(scenario())


def test_failed_warmup_is_visible_to_newly_connected_clients(monkeypatch):
    from wav2sum import daemon

    monkeypatch.setattr(daemon, "_memory_mb", lambda: 0)
    monkeypatch.setattr(daemon, "memory_mb", lambda: 0)

    d = daemon.Daemon(Config())
    d.ready = asyncio.Event()
    d.warm_up_error = "Selected models are unavailable"
    status = d.status()
    assert status["state"] == "failed"
    assert status["error"] == d.warm_up_error


def test_status_discloses_beta_presence_without_reading_keys_or_selecting_cloud(tmp_path, monkeypatch):
    from wav2sum import credentials, local_models

    path = tmp_path / "beta.json"
    path.write_text("private credential bytes that are deliberately not valid JSON")
    monkeypatch.setattr(credentials, "_BETA_PATH", path)
    monkeypatch.setattr(credentials, "Keychain", lambda: pytest.fail("passive status read Keychain"))
    monkeypatch.setattr(settings, "load_preferences", lambda: None)
    monkeypatch.setattr(local_models, "hardware_info", lambda: {})
    monkeypatch.setattr(local_models, "model_status", lambda *_args: {})
    reply = settings.handle({"action": "status"})
    assert reply["bundled_cloud_keys"] is True
    assert reply["settings"]["mode"] == "local"
    assert reply["settings"]["completed"] is False
    assert "private credential bytes" not in json.dumps(reply)
