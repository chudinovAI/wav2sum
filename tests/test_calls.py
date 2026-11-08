import json
import re
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from wav2sum import calls
from wav2sum.audio import SAMPLE_RATE
from wav2sum.calls import CALL, MONO, Names, Speakers, detect_layout, process
from wav2sum.diarizer import Turn
from wav2sum.llm import LLM
from wav2sum.vocabulary import Vocabulary

SR = SAMPLE_RATE
WORDS = {0.3: "привет гитхаб", 0.5: "здравствуй", 0.7: "hello there", 0.9: "good morning"}


class FakeVad:
    def speech_spans(self, audio):
        loud = np.abs(audio) > 0.01
        edges = np.flatnonzero(np.diff(np.r_[0, loud.astype(np.int8), 0]))
        return [(s / SR, e / SR) for s, e in zip(edges[::2], edges[1::2], strict=True)]


class FakeAsr:
    def __init__(self):
        self.calls = 0

    def transcribe_spans(self, audio, spans, language=None, *, terms=()):
        self.calls += 1
        clips = [audio[int(s * SR) : int(e * SR)] for s, e in spans]
        return [WORDS.get(round(float(np.abs(c).mean()), 1), "") for c in clips]


class FakeLLM(LLM):
    def __init__(self):
        super().__init__("fake-model")
        self.requests: list[tuple[str, str]] = []

    def chat(self, system, user, examples=(), max_growth=None, think=None):
        self.requests.append((system, user))
        if think is False:
            return "- 00:03 — Приветствие"
        citation = re.search(r"\[U\d+ [^\]]+\]", user)
        return f"**Тема** — проверка. {citation.group(0)}" if citation else "**Тема** — проверка."

    def count_tokens(self, text):
        return len(text.split())


def fake_models(turns=None):
    asr = FakeAsr()

    def diarize(audio, count, lo, hi):
        if turns is None:
            raise AssertionError("diarizer should not run")
        return turns

    return SimpleNamespace(
        cloud=False,
        vad=FakeVad(),
        call_asr=asr,
        call_session=nullcontext,
        diarizer=SimpleNamespace(diarize=diarize),
    )


def _tone(seconds, amp):
    return np.full(int(seconds * SR), amp, np.float32)


def _recording(tmp_path, monkeypatch, channels, name="call-1.wav", layout=None):
    audio = np.stack(channels, axis=1)
    path = tmp_path / "recordings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio.tobytes())
    if layout:
        path.with_suffix(".json").write_text(json.dumps({"layout": layout}))
    monkeypatch.setattr(calls, "load_audio", lambda p, channels: audio[:, :channels])
    return path


def _call(tmp_path, monkeypatch, mine_amp=0.3, theirs_amp=0.5):
    mic = np.concatenate([_tone(2, mine_amp), np.zeros(4 * SR, np.float32)])
    system = np.concatenate([np.zeros(3 * SR, np.float32), _tone(2, theirs_amp), np.zeros(SR, np.float32)])
    return _recording(tmp_path, monkeypatch, [mic, system], layout=CALL)


def test_call_is_transcribed_and_written_locally(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    vocabulary = Vocabulary()
    vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    llm = FakeLLM()

    result = process(
        audio,
        fake_models(),
        tmp_path / "output",
        llm=llm,
        names=Names("Андрей", "Илья"),
        vocabulary=vocabulary,
        dictionary=["GigaAM"],
    )

    out = tmp_path / "output" / "call-1"
    assert result.out_dir == out
    assert (
        out / "transcript.txt"
    ).read_text() == "[00:00 – 00:02] Андрей: привет GitHub\n\n[00:03 – 00:05] Илья: здравствуй"
    assert not (out / "summary.md").exists()
    meta = json.loads((out / "meta.json").read_text())
    assert meta | {"timings": None} == {
        "audio": str(audio.resolve()),
        "layout": CALL,
        "language": "ru",
        "duration_sec": 6.0,
        "speakers": ["Андрей", "Илья"],
        "model": None,
        "timings": None,
    }
    assert set(meta["timings"]) == {"transcription"}
    assert llm.requests == []


def test_transcription_is_cached_until_settings_change(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    models = fake_models()
    asr = models.call_asr

    process(audio, models, tmp_path / "output", llm=None)
    transcribed = asr.calls
    process(audio, models, tmp_path / "output", llm=None, names=Names("Андрей", "Илья"))
    assert asr.calls == transcribed

    transcript = (tmp_path / "output" / "call-1" / "transcript.txt").read_text()
    assert "Андрей: привет гитхаб" in transcript

    process(audio, models, tmp_path / "output", llm=None, use_cache=False)
    assert asr.calls == 2 * transcribed
    models.diarizer.diarize = lambda *args: [Turn(0, 3.0, 5.0)]
    process(audio, models, tmp_path / "output", llm=None, speakers=Speakers(3))
    assert asr.calls == 3 * transcribed


def test_cache_requires_backend_provenance(tmp_path, monkeypatch):
    from wav2sum.conversation import Utterance

    audio = _call(tmp_path, monkeypatch)
    out = tmp_path / "output"
    local = fake_models()
    process(audio, local, out, llm=None)
    cache = next((out / ".cache").glob("*/utterances.json"))
    payload = json.loads(cache.read_text())
    del payload["params"]["backend"]
    cache.write_text(json.dumps(payload))
    before = local.call_asr.calls
    process(audio, local, out, llm=None)
    assert local.call_asr.calls > before

    seen = []

    def cloud_call(*args, **kwargs):
        seen.append(True)
        return [Utterance(0, 1, "cloud result", "me")]

    cloud = SimpleNamespace(cloud=True, dictation_asr=SimpleNamespace(transcribe_call=cloud_call))
    result = process(audio, cloud, out, llm=None)
    assert seen == [True]
    assert result.utterances[0].text == "cloud result"
    before = local.call_asr.calls
    process(audio, local, out, llm=None)
    assert local.call_asr.calls > before


def test_call_cache_distinguishes_qwen_and_nemotron(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    local = fake_models()
    out = tmp_path / "output"
    local.call_model = "nemotron"
    process(audio, local, out, llm=None)
    first = local.call_asr.calls
    local.call_model = "qwen"
    process(audio, local, out, llm=None)
    assert local.call_asr.calls > first
    payload = json.loads(next((out / ".cache").glob("*/utterances.json")).read_text())
    assert payload["params"]["backend"] == "qwen3-asr-1.7b"


def test_qwen_windows_preserve_speakers_and_limit_duration():
    turns = [calls.Turn(0, 0, 30), calls.Turn(1, 30, 45)]
    pieces = calls._call_windows([(0, 45)], turns, max_seconds=20)
    assert [(p.start, p.end, p.speaker) for p in pieces] == [(0, 20, 0), (20, 30, 0), (30, 45, 1)]


def test_call_worker_closed_on_success_error_and_not_created_on_cache_hit(tmp_path, monkeypatch):
    from wav2sum import models as registry

    audio = _call(tmp_path, monkeypatch)
    made = []

    class Worker(FakeAsr):
        def __init__(self, device):
            super().__init__()
            self.closed = False
            made.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(registry, "NemotronTranscriber", Worker)
    monkeypatch.setattr(registry, "VoiceActivityDetector", FakeVad)
    loaded = registry.Models("cpu", allow_unsafe_nemotron=True)
    out = tmp_path / "output"
    process(audio, loaded, out, llm=None)
    assert len(made) == 1 and made[0].closed
    process(audio, loaded, out, llm=None)
    assert len(made) == 1

    def fail(*args, **kwargs):
        raise RuntimeError("worker failed")

    monkeypatch.setattr(Worker, "transcribe_spans", fail)
    with pytest.raises(RuntimeError, match="worker failed"):
        process(audio, loaded, out, llm=None, use_cache=False)
    assert len(made) == 2 and made[1].closed
    direct_audio = np.stack([_tone(1, 0.3), _tone(1, 0.5)], axis=1)
    with pytest.raises(RuntimeError, match="worker failed"):
        calls.transcribe(direct_audio, CALL, Speakers(), loaded)
    assert len(made) == 3 and made[2].closed


def test_without_llm_there_is_no_summary(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    result = process(audio, fake_models(), tmp_path / "output", llm=None)
    assert result.summary == ""
    assert not (result.out_dir / "summary.md").exists()
    assert json.loads((result.out_dir / "meta.json").read_text())["model"] is None


def test_local_ignores_llm_and_preserves_existing_summary(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    out = tmp_path / "output" / audio.stem
    out.mkdir(parents=True)
    (out / "summary.md").write_text("previous summary")
    llm = FakeLLM()
    result = process(audio, fake_models(), out.parent, llm=llm)
    assert result.summary == ""
    assert llm.requests == []
    assert (out / "summary.md").read_text() == "previous summary"
    assert json.loads((out / "meta.json").read_text())["model"] is None


def test_cloud_whole_call_preserves_speaker_keys_and_timings(tmp_path, monkeypatch):
    from wav2sum.conversation import Utterance

    audio = _call(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(calls, "suppress_echo", lambda mic, system: mic * 0)

    def transcribe_call(data, *, layout, language, diarize, terms=()):
        sent.append((data.copy(), layout, language, diarize))
        return [Utterance(0.2, 1.3, "hello", "me"), Utterance(3.1, 4.5, "hi", "them:1")]

    models = SimpleNamespace(cloud=True, dictation_asr=SimpleNamespace(transcribe_call=transcribe_call))
    llm = FakeLLM()
    result = process(audio, models, tmp_path / "output", llm=llm, language="en", speakers=Speakers(3))
    assert [(u.start, u.end, u.speaker) for u in result.utterances] == [(0.2, 1.3, "Я"), (3.1, 4.5, "Собеседник 2")]
    assert sent[0][1:] == (CALL, "en", True)
    assert not sent[0][0][:, 0].any()
    assert sent[0][0][:, 1].any()
    assert len(llm.requests) == 2
    assert result.summary


def test_cloud_mono_auto_language_and_speaker_mapping(tmp_path, monkeypatch):
    from wav2sum.conversation import Utterance

    audio = _recording(tmp_path, monkeypatch, [_tone(2, 0.3)], name="meeting.wav")
    seen = []

    def transcribe_call(data, *, layout, language, diarize, terms=()):
        seen.append((data.shape, layout, language, diarize))
        return [Utterance(0.1, 1.9, "hello there", "spk:1")]

    models = SimpleNamespace(cloud=True, dictation_asr=SimpleNamespace(transcribe_call=transcribe_call))
    result = process(audio, models, tmp_path / "output", llm=None)
    assert seen == [((2 * SR, 1), MONO, None, True)]
    assert (result.language, result.utterances[0].speaker) == ("en", "Спикер 2")
    assert (result.utterances[0].start, result.utterances[0].end) == (0.1, 1.9)


def test_mono_recording_is_diarized_into_named_speakers(tmp_path, monkeypatch):
    track = np.concatenate([_tone(2, 0.3), np.zeros(SR, np.float32), _tone(2, 0.5)])
    audio = _recording(tmp_path, monkeypatch, [track], name="meeting.wav")
    models = fake_models(turns=[Turn(0, 0.0, 2.0), Turn(1, 3.0, 5.0)])

    result = process(audio, models, tmp_path / "output", llm=None, names=Names(diarized={1: "Илья"}))

    assert result.layout == MONO
    assert [(u.speaker, u.text) for u in result.utterances] == [("Спикер 1", "привет гитхаб"), ("Илья", "здравствуй")]


def test_remote_side_is_diarized_when_more_than_two_people(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    models = fake_models(turns=[Turn(1, 3.0, 5.0)])

    result = process(audio, models, tmp_path / "output", llm=None, speakers=Speakers(3), names=Names("Я", "Они"))

    assert [u.speaker for u in result.utterances] == ["Я", "Они 2"]


def test_auto_language_comes_from_transcribed_text(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch, mine_amp=0.7, theirs_amp=0.9)
    models = fake_models()
    result = process(audio, models, tmp_path / "output", llm=None)
    assert result.language == "en"
    assert [u.text for u in result.utterances] == ["hello there", "good morning"]


@pytest.mark.parametrize(("mine_amp", "language"), [(0.3, "ru"), (0.0, "en")])
def test_each_side_uses_the_same_recognizer_and_text_language(tmp_path, monkeypatch, mine_amp, language):
    audio = _call(tmp_path, monkeypatch, mine_amp=mine_amp, theirs_amp=0.7)
    models = fake_models()

    result = process(audio, models, tmp_path / "output", llm=None, names=Names("Я", "Илья"))

    assert [(u.speaker, u.text) for u in result.utterances] == [("Я", "привет гитхаб")] * bool(mine_amp) + [
        ("Илья", "hello there")
    ]
    assert models.call_asr.calls == (2 if mine_amp else 1)
    assert result.language == language


def test_my_speech_leaking_from_their_side_is_dropped(tmp_path, monkeypatch):
    track = np.concatenate([_tone(2, 0.5), np.zeros(SR, np.float32)])
    audio = _recording(tmp_path, monkeypatch, [track, track], layout=CALL)

    result = process(audio, fake_models(), tmp_path / "output", llm=None, names=Names("Я", "Илья"))

    assert [(u.speaker, u.text) for u in result.utterances] == [("Илья", "здравствуй")]


@pytest.mark.parametrize(
    ("sidecar", "layout"),
    [(None, MONO), ('{"layout": "call"}', CALL), ('{"layout": "mono"}', MONO), ("not json", MONO)],
)
def test_layout_comes_from_the_sidecar(tmp_path, sidecar, layout):
    audio = tmp_path / "call.wav"
    if sidecar is not None:
        audio.with_suffix(".json").write_text(sidecar)
    assert detect_layout(audio) == layout


def test_speaker_counts_without_me():
    assert Speakers(2).without_me().just_one
    assert not Speakers(4).without_me().just_one
    assert Speakers(None, 2, 5).without_me() == Speakers(None, 1, 4)
    assert Speakers(None, max=2).without_me().just_one


def test_names_for_every_kind_of_speaker():
    names = Names("Андрей", "Илья", {0: "Мария"})
    assert [names(k) for k in ("me", "them", "them:0", "them:1", "spk:0", "spk:2")] == [
        "Андрей",
        "Илья",
        "Мария",
        "Илья 2",
        "Мария",
        "Спикер 3",
    ]


def test_local_windows_preserve_short_turns_gaps_and_duration_boundaries():
    from wav2sum.conversation import Piece

    spans = [(0, 12), (13, 25), (26, 60), (61, 62), (63, 64)]
    turns = [Turn(0, 0, 61.2), Turn(1, 61.2, 61.3), Turn(0, 61.3, 64)]
    pieces = calls._call_windows(spans, turns)
    assert [(p.start, p.end, p.speaker) for p in pieces] == [(0, 25, 0), (26, 61.2, 0), (61.2, 61.3, 1), (61.3, 64, 0)]
    assert all(isinstance(p, Piece) and p.end - p.start <= calls.CALL_WINDOW_SEC for p in pieces)
    assert [(p.start, p.end) for p in calls._call_windows([(0, 120)], None)] == [(0, 50), (50, 100), (100, 120)]


def test_uncovered_and_overlapping_turns_are_not_attributed_to_a_speaker():
    turns = [Turn(0, 1, 3), Turn(1, 2, 4), Turn(0, 5, 6)]
    pieces = calls._call_windows([(0, 6)], turns)
    assert [(p.start, p.end, p.speaker) for p in pieces] == [
        (0, 1, None),
        (1, 2, 0),
        (2, 3, None),
        (3, 4, 1),
        (4, 5, None),
        (5, 6, 0),
    ]


def test_windows_do_not_bridge_a_different_speaker_hidden_in_a_vad_gap():
    turns = [Turn(0, 0, 1), Turn(1, 1.1, 1.2), Turn(0, 1.3, 2)]
    pieces = calls._call_windows([(0, 1), (1.3, 2)], turns)
    assert [(p.start, p.end, p.speaker) for p in pieces] == [(0, 1, 0), (1.3, 2, 0)]


def test_windows_keep_long_silences_separate_and_diarization_gaps_unknown():
    pieces = calls._call_windows([(0, 1), (4, 5)], None)
    assert [(p.start, p.end) for p in pieces] == [(0, 1), (4, 5)]
    pieces = calls._call_windows([(0, 2)], [])
    assert [(p.start, p.end, p.speaker) for p in pieces] == [(0, 2, None)]


def test_terms_invalidate_local_cache_but_names_do_not(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    models = fake_models()
    seen = []
    original = models.call_asr.transcribe_spans

    def transcribe(audio, spans, language=None, *, terms=()):
        seen.append(terms)
        return original(audio, spans, language=language, terms=terms)

    models.call_asr.transcribe_spans = transcribe
    vocabulary = Vocabulary()
    vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    out = tmp_path / "output"
    process(audio, models, out, llm=None, dictionary=["Qwen", " qwen "], vocabulary=vocabulary)
    assert seen and all(terms == ("Qwen", "GitHub") for terms in seen)
    count = len(seen)
    process(audio, models, out, llm=None, dictionary=["Qwen"], vocabulary=vocabulary, names=Names("A", "B"))
    assert len(seen) == count
    process(audio, models, out, llm=None, dictionary=["Other"], vocabulary=vocabulary)
    assert len(seen) > count and seen[-1] == ("Other", "GitHub")
    payload = json.loads(next((out / ".cache").glob("*/utterances.json")).read_text())
    assert set(payload) == {"params", "language", "utterances"}


def test_summary_reuses_transcript_without_reading_audio_or_rewriting_result(tmp_path, monkeypatch):
    folder = tmp_path / "existing"
    folder.mkdir()
    transcript = "[00:00 – 00:02] Я: Обновим документацию.\n\n[00:03 – 00:05] Они: Хорошо."
    meta = json.dumps(
        {"audio": str(tmp_path / "missing.wav"), "language": "ru", "model": None, "timings": {"transcription": 1}}
    )
    (folder / "transcript.txt").write_text(transcript)
    (folder / "meta.json").write_text(meta)

    def no_audio(*args, **kwargs):
        pytest.fail("an existing transcript must not be transcribed again")

    monkeypatch.setattr(calls, "load_audio", no_audio)
    llm = FakeLLM()
    assert calls.summarize_existing(folder, llm) == folder
    assert "[U1 00:00–00:02]" in (folder / "summary.md").read_text()
    assert (folder / "transcript.txt").read_text() == transcript
    assert (folder / "meta.json").read_text() == meta
    assert not (tmp_path / "missing.wav").exists()


def test_existing_summary_is_not_replaced_or_sent_to_a_model(tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text("Existing user's summary")
    llm = FakeLLM()
    assert calls.summarize_existing(tmp_path, llm) == tmp_path
    assert summary.read_text() == "Existing user's summary"
    assert llm.requests == []


def test_summary_publish_failure_preserves_transcript_and_leaves_no_partial_output(tmp_path, monkeypatch):
    transcript = "[00:00 – 00:05] Я: Проверим сборку."
    (tmp_path / "transcript.txt").write_text(transcript)
    (tmp_path / "meta.json").write_text('{"language":"ru"}')

    def failed_link(*args, **kwargs):
        raise OSError("test publication failure")

    import os

    monkeypatch.setattr(os, "link", failed_link)
    with pytest.raises(OSError):
        calls.summarize_existing(tmp_path, FakeLLM())
    assert (tmp_path / "transcript.txt").read_text() == transcript
    assert sorted(path.name for path in tmp_path.iterdir()) == ["meta.json", "transcript.txt"]


class FailedOptionalChapters(FakeLLM):
    def chat(self, system, user, examples=(), max_growth=None, think=None):
        if think is False:
            self.requests.append((system, user))
            raise RuntimeError("Cloud provider request failed")
        return super().chat(system, user, examples, max_growth, think)


def test_cloud_chapter_failure_still_publishes_transcript_and_grounded_summary(tmp_path, monkeypatch):
    from wav2sum.conversation import Utterance

    audio = _call(tmp_path, monkeypatch)
    transcribed = []

    def transcribe_call(*args, **kwargs):
        transcribed.append(True)
        return [Utterance(0, 2, "Update the documentation", "me")]

    models = SimpleNamespace(cloud=True, dictation_asr=SimpleNamespace(transcribe_call=transcribe_call))
    llm = FailedOptionalChapters()
    result = process(audio, models, tmp_path / "output", llm=llm)
    assert (result.out_dir / "transcript.txt").read_text() == result.transcript
    assert "[U1 00:00–00:02]" in (result.out_dir / "summary.md").read_text()
    assert (result.out_dir / "meta.json").exists()
    assert len(transcribed) == 1 and len(llm.requests) == 2


def test_existing_transcript_summary_survives_optional_chapter_failure(tmp_path):
    transcript = "[00:00 – 00:02] Я: Обновим документацию."
    metadata = '{"language":"ru","model":null}'
    (tmp_path / "transcript.txt").write_text(transcript)
    (tmp_path / "meta.json").write_text(metadata)
    llm = FailedOptionalChapters()
    assert calls.summarize_existing(tmp_path, llm) == tmp_path
    assert "[U1 00:00–00:02]" in (tmp_path / "summary.md").read_text()
    assert (tmp_path / "transcript.txt").read_text() == transcript
    assert (tmp_path / "meta.json").read_text() == metadata
    assert len(llm.requests) == 2
