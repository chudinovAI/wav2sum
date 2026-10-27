from types import SimpleNamespace

import numpy as np
import pytest

from wav2sum.config import AUTO, DictationConfig
from wav2sum.dictation import (
    CLEANUP_EXAMPLES_RU,
    MAX_GROWTH,
    STYLES_EN,
    DictationStream,
    Dictator,
    expand_snippets,
    fit_to_cursor,
    mark_snippets,
    style_for,
    user_examples,
)
from wav2sum.vad import merge_close_spans
from wav2sum.vocabulary import Vocabulary


def test_style_by_app_prefix_title_and_override():
    assert style_for("ru.keepcoder.Telegram", None, {}) == "chat"
    assert style_for("com.jetbrains.pycharm", None, {}) == "code"
    assert style_for("com.google.Chrome", "Входящие — Gmail", {}) == "email"
    assert style_for("com.google.Chrome", "Новая вкладка", {}) == "default"
    assert style_for("ru.keepcoder.Telegram", None, {"ru.keepcoder.Telegram": "email"}) == "email"


def test_snippets_survive_as_markers():
    text, expansions = mark_snippets("Пиши на мой имейл, ладно?", {"мой имейл": "me@example.com"})
    assert text == "Пиши на ⟦1⟧, ладно?"
    assert expand_snippets(text, expansions) == "Пиши на me@example.com, ладно?"


def test_short_text_skips_llm_and_chat_drops_final_period():
    dictator = Dictator(models=None, cfg=DictationConfig(cleanup=True))
    assert dictator.clean("Привет.", "chat") == "Привет"
    assert dictator.clean("Привет.", "email") == "Привет."


def test_learned_words_fix_text_and_reach_the_prompt_only_as_pairs():
    vocabulary = Vocabulary()
    vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    dictator = Dictator(models=None, cfg=DictationConfig(dictionary=["GigaAM"]), vocabulary=vocabulary)
    assert dictator.clean("На гитхаб.", "chat") == "На GitHub"
    prompt = dictator._prompt("cleanup", "default")
    assert "именно так: GigaAM." in prompt
    assert "«гитхаб» → «GitHub»" in prompt


class FakeAsr:
    def __init__(self, text):
        self.text = text

    def transcribe_spans(self, audio, spans):
        return [self.text for _ in spans]


def fake_models(language):
    asr = {"ru": FakeAsr("привет"), "en": FakeAsr("hello")}
    return SimpleNamespace(
        vad=SimpleNamespace(speech_spans=lambda audio: [(0.0, 1.0)]),
        lid=SimpleNamespace(detect=lambda audio, spans: language),
        asr_for=asr.__getitem__,
    )


def test_detected_language_picks_the_recognizer_and_prompts():
    for language, raw in (("en", "hello"), ("ru", "привет")):
        dictation = Dictator(fake_models(language), DictationConfig(), language=AUTO).dictate(np.zeros(16_000))
        assert (dictation.language, dictation.raw) == (language, raw)


def test_fixed_language_skips_detection():
    models = fake_models("ru")
    models.lid = None
    assert Dictator(models, DictationConfig(), language="en").recognize(np.zeros(16_000)) == ("hello", "en")


def test_english_prompt_is_english():
    dictator = Dictator(models=None, cfg=DictationConfig(dictionary=["GigaAM"]))
    prompt = dictator._prompt("cleanup", "chat", language="en")
    assert "You edit voice input" in prompt and "chat message" in prompt and "GigaAM" in prompt


class ScriptedLLM:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.growths = []
        self.lookups = []

    def chat(self, system, user, examples=(), *, max_growth=None, lookup=False):
        self.requests.append((system, user))
        self.growths.append(max_growth)
        self.lookups.append(lookup)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply(user) if callable(self.reply) else self.reply


def _dictator(reply, **cfg):
    dictator = Dictator(models=None, cfg=DictationConfig(**cfg))
    dictator.llm = ScriptedLLM(reply)
    return dictator


def test_cleanup_keeps_snippets_and_learned_words():
    dictator = _dictator(lambda text: "Короче, " + text.removeprefix("ну "), snippets={"мой имейл": "me@example.com"})
    dictator.vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    assert dictator.clean("ну пиши на мой имейл про гитхаб", "default") == "Короче, пиши на me@example.com про GitHub"
    [(system, user)] = dictator.llm.requests
    assert user == "ну пиши на ⟦1⟧ про гитхаб"
    assert "⟦1⟧" in system


def test_broken_or_runaway_cleanup_falls_back_to_raw_text():
    raw = "ну давай созвонимся в пятницу"
    assert _dictator(TimeoutError("ollama is slow")).clean(raw, "default") == raw
    assert _dictator("").clean(raw, "default") == raw
    assert _dictator("Конечно! Вот письмо: " + "бла " * 50).clean(raw, "default") == raw


def test_cleanup_and_translation_limit_the_reply_but_a_command_does_not():
    dictator = _dictator("Текст")
    dictator.clean("ну давай созвонимся в пятницу", "default")
    dictator.translate("ну давай созвонимся в пятницу", "default")
    dictator.models = fake_models("ru")
    dictator.dictate(np.zeros(16_000), selection="Текст", command=True)
    assert dictator.llm.growths == [MAX_GROWTH, MAX_GROWTH, None]


def test_only_cleanup_drafts_its_reply_from_the_input():
    dictator = _dictator("Текст")
    dictator.clean("ну давай созвонимся в пятницу", "default")
    dictator.translate("ну давай созвонимся в пятницу", "default")
    dictator.models = fake_models("ru")
    dictator.dictate(np.zeros(16_000), selection="Текст", command=True)
    assert dictator.llm.lookups == [True, False, False]


def test_cleanup_off_inserts_raw_text():
    dictator = _dictator(AssertionError("LLM must not run"), cleanup=False)
    assert dictator.clean("ну давай созвонимся в пятницу", "email") == "ну давай созвонимся в пятницу"


def test_voice_command_rewrites_the_selection():
    dictator = _dictator("Short text")
    dictator.models = fake_models("en")
    dictation = dictator.dictate(np.zeros(16_000), selection="A very long text", command=True)
    assert (dictation.text, dictation.command, dictation.language) == ("Short text", True, "ru")
    [(system, user)] = dictator.llm.requests
    assert system.startswith("Ты — помощник по работе с текстом")
    assert user == "Текст:\nA very long text\n\nКоманда: привет"


def test_silence_gives_empty_text_without_llm():
    dictator = _dictator(AssertionError("LLM must not run"))
    dictator.models = fake_models("ru")
    dictator.models.vad.speech_spans = lambda audio: []
    dictator.language = AUTO
    dictation = dictator.dictate(np.zeros(16_000), app="ru.keepcoder.Telegram")
    assert (dictation.raw, dictation.text, dictation.style, dictation.language) == ("", "", "chat", "ru")


def test_mid_sentence_starts_lowercase_after_a_space_and_drops_the_period():
    assert fit_to_cursor("Давай созвонимся.", "Я думаю", " завтра", []) == " давай созвонимся"
    assert fit_to_cursor("Давай созвонимся.", "Я думаю, ", "", []) == "давай созвонимся."
    assert fit_to_cursor("Давай созвонимся.", "Готово.", "", []) == " Давай созвонимся."
    assert fit_to_cursor("Давай созвонимся.", "Готово.\n", " Потом", []) == "Давай созвонимся."
    assert fit_to_cursor("Давай созвонимся.", "Цитата: «", "», ок", []) == "давай созвонимся"
    assert fit_to_cursor("Давай созвонимся...", "", " и всё", []) == "Давай созвонимся..."


def test_cursor_keeps_abbreviations_names_and_english_i():
    assert fit_to_cursor("API готов.", "Слушай,", "", []) == " API готов."
    assert fit_to_cursor("GitHub упал.", "кстати", "", []) == " GitHub упал."
    assert fit_to_cursor("Андрей придёт.", "кстати", "", ["Андрей"]) == " Андрей придёт."
    assert fit_to_cursor("I'm in.", "ok,", "", []) == " I'm in."
    assert fit_to_cursor("Привет.", "", "", []) == "Привет."


def test_dictation_fits_the_cursor_but_a_command_does_not():
    dictator = _dictator("Давай созвонимся.")
    dictator.models = fake_models("ru")
    dictator.models.asr_for = lambda language: FakeAsr("ну давай созвонимся")
    assert dictator.dictate(np.zeros(16_000), before="Я думаю", after=" завтра").text == " давай созвонимся"
    dictator.cfg.dictionary = ["Давай"]
    assert dictator.dictate(np.zeros(16_000), before="Я думаю", after="").text == " Давай созвонимся."
    command = dictator.dictate(np.zeros(16_000), before="Я думаю", after=" завтра", command=True)
    assert command.text == "Давай созвонимся."


def test_snippet_matches_spoken_variants():
    snippets = {"мой адрес": "Москва, Тверская 1", "ещё раз": "again"}
    text, expansions = mark_snippets("Мой, адрес. И еще раз", snippets)
    assert text == "⟦1⟧ И ⟦2⟧"
    assert expand_snippets(text, expansions) == "Москва, Тверская 1 И again"
    assert mark_snippets("мой адресат", snippets) == ("мой адресат", [])


def test_chat_keeps_ellipsis_and_prefix_overrides_apply():
    assert _dictator("Ну и ладно...").clean("ну и ладно короче", "chat") == "Ну и ладно..."
    assert style_for("com.jetbrains.goland", None, {"com.jetbrains.": "notes"}) == "notes"
    assert style_for(None, "Pull request #8 · GitHub", {}) == "code"
    assert style_for(None, None, {}) == "default"


def test_translation_always_goes_to_the_llm_in_english_style():
    dictator = _dictator("Hi.", cleanup=False)
    dictator.models = fake_models("ru")
    dictation = dictator.dictate(np.zeros(16_000), app="ru.keepcoder.Telegram", translate=True)
    assert (dictation.raw, dictation.text, dictation.language, dictation.translate) == ("привет", "Hi", "ru", True)
    [(system, user)] = dictator.llm.requests
    assert system.startswith("You edit voice input and translate it into English")
    assert STYLES_EN["chat"] in system
    assert user == "привет"
    assert dictator.dictate(np.zeros(16_000)).translate is False
    assert len(dictator.llm.requests) == 1


def test_translation_keeps_snippets_and_learned_words():
    dictator = _dictator("Write to ⟦1⟧ about GitHub.", snippets={"мой имейл": "me@example.com"}, dictionary=["GigaAM"])
    dictator.vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    assert dictator.translate("напиши на мой имейл про гитхаб", "default") == "Write to me@example.com about GitHub."
    [(system, user)] = dictator.llm.requests
    assert user == "напиши на ⟦1⟧ про гитхаб"
    assert "Leave markers like ⟦1⟧" in system and "like this: GigaAM." in system and "«гитхаб» → «GitHub»" in system


def test_translation_of_english_speech_is_plain_cleanup():
    dictator = _dictator(AssertionError("LLM must not run"))
    dictator.models = fake_models("en")
    dictator.language = "en"
    assert dictator.dictate(np.zeros(16_000), translate=True).text == "hello"


def test_broken_or_runaway_translation_raises():
    raw = "ну давай созвонимся в пятницу"
    with pytest.raises(TimeoutError):
        _dictator(TimeoutError("ollama is slow")).translate(raw, "default")
    with pytest.raises(ValueError):
        _dictator("").translate(raw, "default")
    with pytest.raises(ValueError):
        _dictator("Sure! Here is the email: " + "bla " * 50).translate(raw, "default")


class SpanAsr:
    """Names each span by its start, and remembers which spans it was asked for."""

    def __init__(self):
        self.calls = []

    def transcribe_spans(self, audio, spans):
        self.calls.append(spans)
        return [f"{start:g}" for start, _ in spans]


def loud_models():
    """VAD that hears every non-zero sample, so a span ends only when silence follows it, like Silero."""

    def speech_spans(audio):
        loud = np.flatnonzero(np.diff(np.concatenate([[0], audio != 0, [0]]).astype(int)))
        spans = [(start / 16_000, end / 16_000) for start, end in zip(loud[::2], loud[1::2], strict=True)]
        return merge_close_spans(spans, 0.6, 20.0)

    asr = SpanAsr()
    lid = SimpleNamespace(calls=[])
    lid.detect = lambda audio, spans: lid.calls.append(spans) or "ru"
    return SimpleNamespace(vad=SimpleNamespace(speech_spans=speech_spans), lid=lid, asr_for=lambda _: asr, asr=asr)


def _speech(*parts):
    """Seconds of speech and silence in turns, starting with speech."""
    return np.concatenate(
        [np.full(int(s * 16_000), 0.5 if i % 2 == 0 else 0.0, np.float32) for i, s in enumerate(parts)]
    )


@pytest.fixture
def models():
    return loud_models()


def test_stream_recognizes_a_phrase_once_the_next_one_starts(models):
    audio = _speech(1, 1, 1, 0.3, 1)
    stream = DictationStream(Dictator(models, DictationConfig(), language="ru"))
    stream.add(audio[:24_000])
    stream.step()
    assert models.asr.calls == []
    stream.add(audio[24_000:40_000])
    stream.step()
    assert models.asr.calls == [[(0.0, 1.0)]]
    stream.add(audio[40_000:])
    assert stream.finish() == ("0 2", "ru")
    assert models.asr.calls == [[(0.0, 1.0)], [(2.0, 4.3)]]
    assert stream.live > 0


def test_streamed_text_matches_the_whole_recording(models):
    audio = _speech(1.2, 0.8, 2, 1, 0.5, 0.7, 3, 0.2)
    dictator = Dictator(models, DictationConfig(), language="ru")
    stream = DictationStream(dictator)
    for start in range(0, len(audio), 16_000):
        stream.add(audio[start : start + 16_000])
        stream.step()
    assert stream.finish() == dictator.recognize(audio)


def test_auto_language_waits_for_three_seconds_of_finished_speech(models):
    audio = _speech(2, 1, 2, 1, 1)
    stream = DictationStream(Dictator(models, DictationConfig(), language=AUTO))
    stream.add(audio[: 4 * 16_000])
    stream.step()
    assert (models.lid.calls, models.asr.calls) == ([], [])
    stream.add(audio[4 * 16_000 :])
    stream.step()
    assert (models.lid.calls, models.asr.calls) == ([[(0.0, 2.0), (3.0, 5.0)]], [[(0.0, 2.0), (3.0, 5.0)]])
    assert stream.finish() == ("0 3 6", "ru")
    assert len(models.lid.calls) == 1


def test_short_auto_dictation_detects_the_language_at_the_end(models):
    stream = DictationStream(Dictator(models, DictationConfig(), language=AUTO))
    stream.add(_speech(1, 1, 1))
    stream.step()
    assert stream.finish() == ("0 2", "ru")
    assert models.lid.calls == [[(0.0, 1.0), (2.0, 3.0)]]


def test_streamed_dictation_reports_live_and_final_asr(models):
    dictator = _dictator("Текст", cleanup=False)
    dictator.models = models
    stream = DictationStream(dictator)
    stream.add(_speech(1, 1, 1))
    stream.step()
    dictation = dictator.dictate(stream.audio, stream=stream)
    assert dictation.raw == "0 2"
    assert set(dictation.timings) == {"asr", "asr_live", "llm"}


def test_user_examples_take_the_3_latest_edits_of_each_style_and_language():
    def edit(raw, style="chat", language="ru"):
        return {"raw": raw, "edited": raw.upper(), "style": style, "language": language}

    edits = [edit("пять"), edit("hi", language="en"), edit("четыре"), edit("три", style="email"), edit("два")]
    edits += [edit("один"), {**edit("переписал"), "edited": "x" * 100}]
    assert user_examples(edits) == {
        ("chat", "ru"): [("два", "ДВА"), ("четыре", "ЧЕТЫРЕ"), ("пять", "ПЯТЬ")],
        ("chat", "en"): [("hi", "HI")],
        ("email", "ru"): [("три", "ТРИ")],
    }


def test_cleanup_uses_the_users_examples_of_its_style_and_language():
    calls = []
    dictator = Dictator(models=None, cfg=DictationConfig(), examples={("chat", "ru"): [("ну да", "Да —")]})
    dictator.llm = SimpleNamespace(chat=lambda system, text, examples, **kw: calls.append(examples) or text)
    dictator.clean("ну да конечно", "chat")
    dictator.clean("ну да конечно", "email")
    dictator.warm_llm()
    assert calls == [[("ну да", "Да —")], CLEANUP_EXAMPLES_RU, CLEANUP_EXAMPLES_RU]
