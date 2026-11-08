import getpass
import json
from types import SimpleNamespace

import pytest

from wav2sum import credentials


@pytest.fixture
def store(monkeypatch, tmp_path):
    values = {}
    writes = []

    class Keychain:
        def __init__(self, **kwargs):
            pass

        def get(self, name):
            return values.get(name)

        def set(self, name, value):
            writes.append((name, value))
            values[name] = value

    monkeypatch.setattr(credentials, "Keychain", Keychain)
    monkeypatch.setattr(credentials, "_BETA_PATH", tmp_path / "beta.json", raising=False)
    for name in credentials.PROVIDERS:
        monkeypatch.delenv(name, raising=False)
    return values, writes


def test_environment_override_never_reads_keychain(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "  temporary-override  ")
    monkeypatch.setattr(credentials, "Keychain", lambda **kw: pytest.fail("override must not access Keychain"))
    assert credentials.credential("DEEPGRAM_API_KEY") == "temporary-override"


def test_missing_environment_reads_saved_key_without_export(store):
    values, _ = store
    values["DEEPGRAM_API_KEY"] = "saved-secret"
    assert credentials.credential("DEEPGRAM_API_KEY") == "saved-secret"


def test_empty_environment_does_not_hide_saved_value(store, monkeypatch):
    values, _ = store
    values["OPENAI_API_KEY"] = "saved-openai"
    monkeypatch.setenv("OPENAI_API_KEY", "  ")
    assert credentials.credential("OPENAI_API_KEY") == "saved-openai"


def test_missing_key_explains_one_time_setup(store):
    with pytest.raises(RuntimeError, match=r"DEEPGRAM_API_KEY.*cloud-keys"):
        credentials.credential("DEEPGRAM_API_KEY")


def test_keychain_denial_is_not_treated_as_missing_or_other_provider(store, monkeypatch):
    class Denied:
        def __init__(self, **kwargs):
            pass

        def get(self, name):
            raise RuntimeError("Keychain access unavailable (status -25308)")

    monkeypatch.setattr(credentials, "Keychain", Denied)
    with pytest.raises(RuntimeError, match="Keychain access unavailable"):
        credentials.credential("OPENAI_API_KEY")


def test_setup_saves_existing_exports_without_reading_tty_or_printing_secrets(store, monkeypatch, capsys):
    values, writes = store
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-existing-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-existing-secret")
    monkeypatch.setattr(getpass, "getpass", lambda *a, **kw: pytest.fail("exports already supplied"))
    credentials.configure()
    assert values == {"DEEPGRAM_API_KEY": "dg-existing-secret", "OPENAI_API_KEY": "oa-existing-secret"}
    assert len(writes) == 2
    output = capsys.readouterr()
    assert "dg-existing-secret" not in output.out + output.err
    assert "oa-existing-secret" not in output.out + output.err
    assert "Keychain" in output.out


def test_setup_hidden_input_is_saved_for_later_lookup(store, monkeypatch):
    _, _writes = store
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(["  dg-entered  ", "oa-entered"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    credentials.configure()
    assert credentials.credential("DEEPGRAM_API_KEY") == "dg-entered"
    assert credentials.credential("OPENAI_API_KEY") == "oa-entered"


@pytest.mark.parametrize("answer", ["", "  ", EOFError(), KeyboardInterrupt()])
def test_empty_or_interrupted_setup_does_not_overwrite_any_item(store, monkeypatch, answer):
    values, writes = store
    values.update(DEEPGRAM_API_KEY="old-deepgram", OPENAI_API_KEY="old-openai")
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    responses = iter(["new-deepgram", answer])

    def prompt(_label):
        value = next(responses)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(getpass, "getpass", prompt)
    with pytest.raises((ValueError, EOFError, KeyboardInterrupt)):
        credentials.configure()
    assert not writes
    assert values == {"DEEPGRAM_API_KEY": "old-deepgram", "OPENAI_API_KEY": "old-openai"}


def test_noninteractive_setup_never_falls_back_to_echoed_input(store, monkeypatch):
    _, writes = store
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(getpass, "getpass", lambda *a: pytest.fail("must not echo in redirected stdin"))
    with pytest.raises(RuntimeError, match="interactive terminal"):
        credentials.configure()
    assert not writes


def test_secure_input_failure_never_falls_back_to_echo_or_saves(store, monkeypatch):
    import warnings

    _, writes = store
    monkeypatch.setattr(credentials.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def insecure_input(_prompt):
        warnings.warn("cannot disable echo", getpass.GetPassWarning, stacklevel=1)
        pytest.fail("must stop before reading plaintext")

    monkeypatch.setattr(getpass, "getpass", insecure_input)
    with pytest.raises(RuntimeError, match="Secure hidden input"):
        credentials.configure()
    assert not writes


def test_local_warmup_never_accesses_keychain(monkeypatch):
    from wav2sum import models

    def forbidden_keychain(**_kwargs):
        pytest.fail("Local processing must never access Keychain")

    monkeypatch.setattr(credentials, "Keychain", forbidden_keychain)
    monkeypatch.setattr(models, "QwenTranscriber", lambda _device: SimpleNamespace(transcribe=lambda _audio: ""))
    monkeypatch.setattr(models, "VoiceActivityDetector", lambda: SimpleNamespace(speech_spans=lambda _audio: []))
    models.Models(device="cpu").preload_dictation()


def test_beta_keys_work_without_saved_items_but_respect_user_overrides(store, monkeypatch):
    values, writes = store
    credentials._BETA_PATH.write_text(json.dumps({"DEEPGRAM_API_KEY": "beta-dg", "OPENAI_API_KEY": "beta-oa"}))
    assert credentials.credential("DEEPGRAM_API_KEY") == "beta-dg"
    assert credentials.credential("OPENAI_API_KEY") == "beta-oa"
    values["OPENAI_API_KEY"] = "own-key"
    assert credentials.credential("OPENAI_API_KEY") == "own-key"
    monkeypatch.setenv("OPENAI_API_KEY", "explicit-key")
    assert credentials.credential("OPENAI_API_KEY") == "explicit-key"
    assert not writes


@pytest.mark.parametrize("contents", ['{"OPENAI_API_KEY":"only-one"}', '{"secret":"never-print-this"}', "broken"])
def test_invalid_beta_keys_fail_closed_without_exposing_contents(store, contents):
    credentials._BETA_PATH.write_text(contents)
    with pytest.raises(RuntimeError, match="Bundled beta credentials are invalid") as error:
        credentials.credential("DEEPGRAM_API_KEY")
    assert contents not in str(error.value)


def test_beta_keys_do_not_bypass_keychain_denial(store, monkeypatch):
    credentials._BETA_PATH.write_text(json.dumps({"DEEPGRAM_API_KEY": "beta-dg", "OPENAI_API_KEY": "beta-oa"}))

    class Denied:
        def get(self, _name):
            raise RuntimeError("Keychain access unavailable")

    monkeypatch.setattr(credentials, "Keychain", Denied)
    with pytest.raises(RuntimeError, match="Keychain access unavailable"):
        credentials.credential("OPENAI_API_KEY")
