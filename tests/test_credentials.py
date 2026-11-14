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


def test_keychain_denial_is_not_treated_as_missing_or_other_provider(store, monkeypatch):
    class Denied:
        def __init__(self, **kwargs):
            pass

        def get(self, name):
            raise RuntimeError("Keychain access unavailable (status -25308)")

    monkeypatch.setattr(credentials, "Keychain", Denied)
    with pytest.raises(RuntimeError, match="Keychain access unavailable"):
        credentials.credential("OPENAI_API_KEY")


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
