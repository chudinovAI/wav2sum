import sys
from contextlib import contextmanager

import pytest

from wav2sum import __main__ as runtime
from wav2sum import config, daemon, settings
from wav2sum.config import Config


@pytest.mark.parametrize(("args", "cloud"), [(["serve"], False), (["serve", "--cloud"], True)])
def test_serve_loads_config_and_starts_under_exclusive_lock(args, cloud, tmp_path, monkeypatch):
    cfg = Config()
    cfg.cloud = not cloud
    seen = []

    @contextmanager
    def exclusive(path, *, wait):
        seen.append((path, wait))
        yield True

    monkeypatch.setattr(sys, "argv", ["wav2sum", *args])
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(config, "exclusive", exclusive)
    monkeypatch.setattr(daemon, "serve", lambda selected, verbose: seen.append((selected, verbose)))
    runtime.main()
    assert cfg.cloud is cloud
    assert seen == [(tmp_path / "daemon.lock", False), (cfg, False)]


def test_setup_dispatches_without_loading_config(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["wav2sum", "setup"])
    monkeypatch.setattr(config, "load_config", lambda: pytest.fail("setup must not load config"))
    monkeypatch.setattr(settings, "main", lambda: calls.append("setup"))
    runtime.main()
    assert calls == ["setup"]


def test_serve_does_not_start_if_lock_is_held(tmp_path, monkeypatch):
    @contextmanager
    def exclusive(path, *, wait):
        yield False

    monkeypatch.setattr(sys, "argv", ["wav2sum", "serve"])
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "load_config", Config)
    monkeypatch.setattr(config, "exclusive", exclusive)
    monkeypatch.setattr(daemon, "serve", lambda *args: pytest.fail("daemon must not start"))
    with pytest.raises(SystemExit, match="already running"):
        runtime.main()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["app"],
        ["tui"],
        ["status"],
        ["stop"],
        ["record"],
        ["mics"],
        ["dictate"],
        ["cloud-keys"],
        ["transcribe", "synthetic.wav"],
        ["synthetic.wav"],
        ["--cloud", "serve"],
        ["setup", "--cloud"],
        ["serve", "--verbose"],
        ["serve", "--cloud", "extra"],
    ],
)
def test_other_commands_are_rejected_before_loading_config(args, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wav2sum", *args])
    monkeypatch.setattr(config, "load_config", lambda: pytest.fail("invalid command must not load config"))
    with pytest.raises(SystemExit):
        runtime.main()
