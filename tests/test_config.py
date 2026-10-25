import tomllib
from pathlib import Path

import pytest

from wav2sum.config import Config, _toml, exclusive, load_config, write_default_config


def test_default_config_round_trips(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# placeholder\n")
    write_default_config(path)
    assert tomllib.loads(path.read_text())["dictation"]["hotkey"] == "right_option"
    assert load_config(path) == Config()


def test_existing_settings_are_kept(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('me = "Андрей"\n')
    write_default_config(path)
    assert path.read_text() == 'me = "Андрей"\n'


def test_exclusive_lock_admits_one_holder(tmp_path):
    lock = tmp_path / "daemon.lock"
    with exclusive(lock, wait=False) as first, exclusive(lock, wait=False) as second:
        assert (first, second) == (True, False)
    with exclusive(lock, wait=False) as again:
        assert again


def test_settings_load_with_unknown_keys_ignored(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text(
        'me = "Андрей"\nrecordings_dir = "~/calls"\ncolour = "red"\n\n'
        '[dictation]\nlearn = false\nsnippets = { "мой имейл" = "me@example.com" }\nvoice = "deep"\n'
    )
    cfg = load_config(path)
    assert cfg.me == "Андрей"
    assert cfg.recordings_dir == Path.home() / "calls"
    assert not cfg.dictation.learn
    assert cfg.dictation.snippets == {"мой имейл": "me@example.com"}
    assert "unknown setting colour" in caplog.text and "unknown setting dictation.voice" in caplog.text


def test_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.toml") == Config()


@pytest.mark.parametrize(
    "value",
    [True, 16384, 0.5, "Андрей", 'a\\b "c"', ["us.zoom", "Cisco-Systems.Spark"], {'"кавычки"': "me@example.com"}],
)
def test_default_values_are_valid_toml(value):
    assert tomllib.loads(f"x = {_toml(value)}")["x"] == value
