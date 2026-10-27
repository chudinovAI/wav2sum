from types import SimpleNamespace

import numpy as np
import pytest

from wav2sum.config import AUTO, DictationConfig
from wav2sum.dictation import STYLES_EN, Dictator, expand_snippets, mark_snippets, style_for
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

    def chat(self, system, user, examples=()):
        self.requests.append((system, user))
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
