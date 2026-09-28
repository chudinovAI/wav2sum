import tomllib

from wav2sum.config import Config, load_config, write_default_config


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
