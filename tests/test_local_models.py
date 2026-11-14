import pytest

from wav2sum import local_models


def test_recommendation_is_provisional_and_conservative():
    for memory in (16, 24, 32, 48):
        rec = local_models.recommendation({"memory_gib": memory})
        assert rec["call_model"] == ("nemotron" if memory >= 48 else "qwen")
        assert rec["qualification"] == "provisional"
    assert local_models.recommendation({"memory_gib": None})["call_model"] == "qwen"


def test_nemotron_guard_requires_explicit_override():
    with pytest.raises(RuntimeError, match="Qwen"):
        local_models.check_memory("nemotron", hardware={"memory_gib": 16})
    local_models.check_memory("nemotron", hardware={"memory_gib": 16}, allow_unsafe=True)
    local_models.check_memory("qwen", hardware={"memory_gib": 16})


def test_prepare_rejects_unknown_model_without_network():
    with pytest.raises(ValueError):
        local_models.prepare_models("other", local_models.CLEANUP_MODEL)
    with pytest.raises(ValueError):
        local_models.prepare_models("qwen", "unknown/model")


def test_prepare_passes_token_only_to_gated_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(local_models, "_snapshot", lambda repo, revision, **kw: calls.append((repo, revision, kw)))
    monkeypatch.setattr(local_models, "model_status", lambda *args: {})
    local_models.prepare_models("qwen", local_models.CLEANUP_MODEL, hf_token="secret")
    assert len(calls) == 3
    assert calls[0][2].get("token") is False
    assert calls[1][2].get("token") is False
    assert calls[2][2]["token"] == "secret"


def test_gated_failure_does_not_expose_token(monkeypatch):
    def snapshot(repo, revision, **kwargs):
        if repo == local_models.DIARIZATION_MODEL:
            raise ValueError(f"bad token {kwargs['token']}")

    monkeypatch.setattr(local_models, "_snapshot", snapshot)
    with pytest.raises(RuntimeError, match="Accept") as error:
        local_models.prepare_models("qwen", local_models.CLEANUP_MODEL, hf_token="secret")
    assert "secret" not in str(error.value)


def test_status_is_offline_and_never_asks_for_token(monkeypatch, tmp_path):
    calls = []

    def snapshot(repo, revision, **kwargs):
        calls.append(kwargs)
        return tmp_path

    monkeypatch.setattr(local_models, "_snapshot", snapshot)
    status = local_models.model_status("qwen", local_models.CLEANUP_MODEL)
    assert not status["qwen"]
    assert not status["diarization"]
    assert all(call == {"local_files_only": True, "token": False} for call in calls)


def test_disabled_calls_prepares_only_dictation_models_and_status_does_not_touch_calls(monkeypatch, tmp_path):
    seen = []

    def snapshot(repo, revision, **kwargs):
        seen.append((repo, kwargs))
        return tmp_path

    monkeypatch.setattr(local_models, "_snapshot", snapshot)
    monkeypatch.setattr(local_models, "_prepare_speech", lambda: pytest.fail("prepared speech runtime"))
    monkeypatch.setattr(local_models, "hf_hub_download", lambda *a, **kw: pytest.fail("downloaded Nemotron"))
    monkeypatch.setattr(local_models, "speech_paths", lambda: pytest.fail("checked call runtime"))
    status = local_models.prepare_models("nemotron", local_models.CLEANUP_MODEL, hf_token="secret", calls_enabled=False)
    assert [repo for repo, _ in seen] == [
        local_models.QWEN_MODEL,
        local_models.CLEANUP_MODEL,
        local_models.QWEN_MODEL,
        local_models.CLEANUP_MODEL,
    ]
    assert seen[2][1] == {"local_files_only": True, "token": False}
    assert status["nemotron"] is False and status["diarization"] is False
