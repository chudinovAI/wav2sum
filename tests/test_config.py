import tomllib

from wav2sum.config import Config, exclusive, load_config, write_default_config


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
