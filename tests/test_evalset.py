import numpy as np

from wav2sum import evalset
from wav2sum.evalset import PHRASES, _edits, _has, record, report


def test_edits_count_substitutions_insertions_and_deletions():
    assert _edits(["a", "b", "c"], ["a", "b", "c"]) == 0
    assert _edits(["a", "b", "c"], ["a", "x", "c"]) == 1
    assert _edits(["a", "b", "c"], ["a", "c"]) == 1
    assert _edits(["a", "b"], ["a", "b", "c", "d"]) == 2
    assert _edits([], ["a"]) == 1


def test_term_needs_its_writing_but_not_its_case_or_punctuation():
    assert _has("Запушь в Main.", "main")
    assert not _has("Запушь в мейн.", "main")
    assert not _has("domain", "main")
    assert _has("Открой vs code, пожалуйста", "VS Code")
    assert not _has("Открой VS и Code", "VS Code")


def test_phrases_have_their_terms():
    assert 40 <= len(PHRASES) <= 50
    for text, terms in PHRASES:
        assert all(_has(text, term) for term in terms), text


class FakeDictator:
    def __init__(self, raw, clean):
        self.raw, self.clean_text, self.cleaned = raw, clean, []

    def recognize(self, audio):
        return self.raw[round(audio[0])], "ru"

    def clean(self, raw, style, language):
        self.cleaned.append((raw, style, language))
        return self.clean_text[raw]


def test_report_per_condition_asr_and_cleanup_apart(tmp_path, monkeypatch):
    # The audio's first sample says which phrase it is.
    monkeypatch.setattr(evalset, "PHRASES", [("Запушь в main.", ["main"]), ("Привет, как дела?", [])])
    monkeypatch.setattr(evalset, "load_audio", lambda path, channels: np.full((10, 1), int(path.stem) - 1.0))
    for name in ["quiet/01.wav", "quiet/02.wav", "noise/02.wav"]:
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).touch()
    dictator = FakeDictator(
        raw=["запушь в мейн", "привет как дела"],
        clean={"запушь в мейн": "Запушь в main.", "привет как дела": "Привет."},
    )
    seeds = []

    text = report(dictator, tmp_path, seed=lambda: seeds.append(1))

    # 1 of 6 words wrong before the cleanup, 2 of 6 after it; the term is right only after it.
    assert "| quiet | 2 | 16.7% | 33.3% | 0/1 | 1/1 |" in text
    assert "| noise | 1 | 0.0% | 66.7% | 0/0 | 0/0 |" in text
    assert "airpods" not in text
    assert dictator.cleaned[0] == ("запушь в мейн", "default", "ru")
    assert len(seeds) == 3


def test_record_skips_recorded_phrases_and_keeps_only_finished_files(tmp_path, monkeypatch):
    monkeypatch.setattr(evalset, "PHRASES", [("раз", []), ("два", []), ("три", [])])
    (tmp_path / "01.wav").write_bytes(b"old")
    prompts, started = [], []

    class FakeProc:
        def __init__(self, cmd, stdin):
            self.part = cmd[-1]
            started.append(cmd)

        def communicate(self, data):
            assert data == b"q"
            with open(self.part, "w") as f:
                f.write("new")
            self.returncode = 1 if len(started) == 2 else 0

    monkeypatch.setattr(evalset.subprocess, "Popen", FakeProc)
    monkeypatch.setattr("builtins.input", prompts.append)

    record(tmp_path)

    assert (tmp_path / "01.wav").read_bytes() == b"old"
    assert (tmp_path / "02.wav").read_text() == "new"
    # ffmpeg failed on the third phrase: no .wav, so the next run asks for it again.
    assert not (tmp_path / "03.wav").exists()
    assert len(started) == 2
    assert "[2/3] два" in prompts[0]
    assert "-ar" in started[0] and "16000" in started[0]
