from types import SimpleNamespace

import numpy as np

from wav2sum.config import AUTO, DictationConfig
from wav2sum.dictation import Dictator, expand_snippets, mark_snippets, style_for
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


def test_learned_words_fix_text_and_reach_the_prompt():
    vocabulary = Vocabulary()
    vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    dictator = Dictator(models=None, cfg=DictationConfig(dictionary=["GigaAM"]), vocabulary=vocabulary)
    assert dictator.clean("На гитхаб.", "chat") == "На GitHub"
    prompt = dictator._prompt("cleanup", "default")
    assert "GigaAM, GitHub." in prompt
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
