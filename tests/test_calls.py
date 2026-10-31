import json
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

    def transcribe_spans(self, audio, spans):
        self.calls += 1
        clips = [audio[int(s * SR) : int(e * SR)] for s, e in spans]
        return [WORDS.get(round(float(np.abs(c).mean()), 1), "") for c in clips]


class FakeLLM(LLM):
    def __init__(self):
        super().__init__("fake-model")
        self.requests: list[tuple[str, str]] = []

    def chat(self, system, user, examples=(), max_growth=None, think=None):
        self.requests.append((system, user))
        return "- 00:03 — Приветствие" if think is False else "**Тема** — проверка."

    def count_tokens(self, text):
        return len(text.split())


def fake_models(language="ru", turns=None):
    asr = {"ru": FakeAsr(), "en": FakeAsr()}

    def diarize(audio, count, lo, hi):
        if turns is None:
            raise AssertionError("diarizer should not run")
        return turns

    return SimpleNamespace(
        vad=FakeVad(),
        lid=SimpleNamespace(detect=language if callable(language) else lambda audio, spans: language),
        asr_for=asr.__getitem__,
        asr=asr,
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


def test_call_is_transcribed_summarized_and_written(tmp_path, monkeypatch):
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
    assert (out / "summary.md").read_text() == "**Тема** — проверка.\n\n**Главы**\n- 00:03 — Приветствие"
    meta = json.loads((out / "meta.json").read_text())
    assert meta | {"timings": None} == {
        "audio": str(audio.resolve()),
        "layout": CALL,
        "language": "ru",
        "duration_sec": 6.0,
        "speakers": ["Андрей", "Илья"],
        "model": "fake-model",
        "timings": None,
    }
    assert set(meta["timings"]) == {"transcription", "summarization"}

    [(system, user), (_, chapters_user)] = llm.requests
    assert "GigaAM, GitHub" in system
    assert user.endswith("Андрей: привет GitHub\n\nИлья: здравствуй")
    assert chapters_user == "[00:00 – 00:02] Андрей: привет GitHub\n\n[00:03 – 00:05] Илья: здравствуй"


def test_transcription_is_cached_until_settings_change(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    models = fake_models()
    asr = models.asr["ru"]

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


def test_without_llm_there_is_no_summary(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    result = process(audio, fake_models(), tmp_path / "output", llm=None)
    assert result.summary == ""
    assert not (result.out_dir / "summary.md").exists()
    assert json.loads((result.out_dir / "meta.json").read_text())["model"] is None


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


def test_one_person_found_in_a_range_keeps_the_plain_name(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    models = fake_models(turns=[Turn(0, 3.0, 5.0)])

    result = process(
        audio, models, tmp_path / "output", llm=None, speakers=Speakers(None, 2, 7), names=Names("Я", "Илья")
    )

    assert [u.speaker for u in result.utterances] == ["Я", "Илья"]


@pytest.mark.parametrize("speaker_id", [0, 2])
def test_single_diarized_speaker_respects_names_even_when_using_cache(tmp_path, monkeypatch, speaker_id):
    audio = _call(tmp_path, monkeypatch)
    models = fake_models(turns=[Turn(speaker_id, 3.0, 5.0)])
    speakers = Speakers(None, 2, 7)
    kwargs = {"llm": None, "speakers": speakers}

    first = process(audio, models, tmp_path / "output", names=Names("Я", "Илья"), **kwargs)
    calls_before = models.asr["ru"].calls
    second = process(audio, models, tmp_path / "output", names=Names("Я", "Илья", {speaker_id: "Алексей"}), **kwargs)
    third = process(audio, models, tmp_path / "output", names=Names("Я", "Друг"), **kwargs)

    assert [u.speaker for u in first.utterances] == ["Я", "Илья"]
    assert [u.speaker for u in second.utterances] == ["Я", "Алексей"]
    assert [u.speaker for u in third.utterances] == ["Я", "Друг"]
    assert models.asr["ru"].calls == calls_before


def test_multiple_diarized_speakers_in_a_range_remain_numbered(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    models = fake_models(turns=[Turn(0, 3.0, 4.0), Turn(1, 4.0, 5.0)])
    result = process(audio, models, tmp_path / "output", llm=None, speakers=Speakers(None, 2, 7))
    assert [u.speaker for u in result.utterances] == ["Я", "Собеседник 1", "Собеседник 2"]


def test_single_speaker_cache_from_v8_is_recomputed(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch)
    models = fake_models(turns=[Turn(0, 3.0, 5.0)])
    output = tmp_path / "output"
    kwargs = {"llm": None, "speakers": Speakers(None, 2, 7), "names": Names(diarized={0: "Алексей"})}
    process(audio, models, output, **kwargs)
    cached = next((output / ".cache").glob("*/utterances.json"))
    payload = json.loads(cached.read_text())
    payload["params"]["version"] = 8
    for utterance in payload["utterances"]:
        if utterance["speaker"].startswith("single-remote:"):
            utterance["speaker"] = "them"
    cached.write_text(json.dumps(payload))
    before = models.asr["ru"].calls

    result = process(audio, models, output, **kwargs)
    assert [u.speaker for u in result.utterances] == ["Я", "Алексей"]
    assert models.asr["ru"].calls > before


def test_detected_language_picks_recognizer_and_summary_language(tmp_path, monkeypatch):
    audio = _call(tmp_path, monkeypatch, mine_amp=0.7, theirs_amp=0.9)
    models = fake_models(language="en")
    llm = FakeLLM()

    result = process(audio, models, tmp_path / "output", llm=llm)

    assert result.language == "en"
    assert models.asr["ru"].calls == 0
    assert [u.text for u in result.utterances] == ["hello there", "good morning"]
    assert llm.requests[0][0].startswith("You write a summary")


def _language_by_loudness(audio, spans):
    return "en" if np.abs(audio).max() > 0.6 else "ru"


@pytest.mark.parametrize(("mine_amp", "language"), [(0.3, "ru"), (0.0, "en")])
def test_each_side_of_a_call_gets_its_own_recognizer(tmp_path, monkeypatch, mine_amp, language):
    audio = _call(tmp_path, monkeypatch, mine_amp=mine_amp, theirs_amp=0.7)
    models = fake_models(language=_language_by_loudness)
    llm = FakeLLM()

    result = process(audio, models, tmp_path / "output", llm=llm, names=Names("Я", "Илья"))

    assert [(u.speaker, u.text) for u in result.utterances] == [("Я", "привет гитхаб")] * bool(mine_amp) + [
        ("Илья", "hello there")
    ]
    assert models.asr["en"].calls == 1
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
