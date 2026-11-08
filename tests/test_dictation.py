from types import SimpleNamespace

import numpy as np
import pytest
from audio_fakes import FakeVAD

from wav2sum.config import AUTO, DictationConfig
from wav2sum.dictation import (
    STYLES_EN,
    Dictator,
    expand_snippets,
    fit_to_cursor,
    mark_snippets,
    style_for,
    user_examples,
)
from wav2sum.language import text_language
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


def test_short_text_goes_to_llm_and_chat_drops_final_period():
    dictator = _dictator("Привет.")
    assert dictator.clean("привет", "chat") == "Привет"
    assert dictator.clean("привет", "email") == "Привет."
    assert [user for _, user in dictator.llm.requests] == ["привет", "привет"]


def test_learned_words_fix_text_and_reach_the_prompt_only_as_pairs():
    vocabulary = Vocabulary()
    vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    dictator = Dictator(models=None, cfg=DictationConfig(dictionary=["GigaAM"]), vocabulary=vocabulary)
    dictator.llm = ScriptedLLM(lambda text: text)
    assert dictator.clean("На гитхаб.", "chat") == "На GitHub"
    prompt = dictator._prompt("cleanup", "default")
    assert "именно так: GigaAM." in prompt
    assert "«гитхаб» → «GitHub»" in prompt


class FakeAsr:
    def __init__(self, text):
        self.text = text
        self.languages = []

    def transcribe_spans(self, audio, spans, language=None, *, terms=()):
        self.languages.append(language)
        return [self.text for _ in spans]


def fake_models(language):
    asr = FakeAsr("привет" if language == "ru" else "hello")
    return SimpleNamespace(vad=FakeVAD(lambda audio: [(0.0, 1.0)]), dictation_asr=asr)


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("hello there friend", "en"),
        ("привет всем друзьям", "ru"),
        ("Проверь Python Script работает", "ru"),
    ],
)
def test_auto_uses_final_text_language_for_cleanup_and_command(text, language):
    dictator = _dictator(text)
    dictator.models = fake_models("ru")
    dictator.models.dictation_asr.text = text
    dictator.language = AUTO
    result = dictator.dictate(np.zeros(16_000))
    assert (result.raw, result.language) == (text, language)
    assert dictator.models.dictation_asr.languages == [None]
    system, _ = dictator.llm.requests[-1]
    assert system.startswith("You edit voice input" if language == "en" else "Ты")
    dictator.dictate(np.zeros(16_000), command=True)
    system, _ = dictator.llm.requests[-1]
    assert system.startswith("You help with text" if language == "en" else "Ты")


def test_fixed_language_is_forwarded_and_retained():
    models = fake_models("ru")
    assert Dictator(models, DictationConfig(), language="en").recognize(np.zeros(16_000)) == ("привет", "en")
    assert models.dictation_asr.languages == ["en"]


def test_warm_up_loads_dictation_models_and_cleanup_llm():
    calls = []
    dictator = Dictator(SimpleNamespace(preload_dictation=lambda: calls.append("models")), DictationConfig())
    dictator.warm_llm = lambda: calls.append("llm")
    dictator.warm_up()
    assert calls == ["models", "llm"]


def test_cloud_warmup_validates_credentials_without_paid_request(monkeypatch):
    from wav2sum import dictation

    calls = []

    class CloudLLM:
        def __init__(self, **kwargs):
            pass

        def validate_credentials(self):
            calls.append("llm credentials")

        def chat(self, *args, **kwargs):
            pytest.fail("warmup must not call cloud LLM")

    monkeypatch.setattr(dictation, "OpenAILLM", CloudLLM)
    models = SimpleNamespace(cloud=True, preload_dictation=lambda: calls.append("asr credentials"))
    Dictator(models, DictationConfig()).warm_up()
    assert calls == ["asr credentials", "llm credentials"]


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


def test_cleanup_sends_recorded_self_correction_unchanged_and_accepts_llm_result():
    raw = (
        "Завтра сезон в 15:30. Нет, перенеси на 16 30. После их встречи создай полуреквест на GitHub "
        "и отправи ссылку в Slack. Please review the changes before merging into Main. "
        "Bюджет двенадцать долларов пятьдесят центов. Документы пока не отправляй. "
        "Сначала дождись нового подтверждения."
    )
    corrected = (
        "Завтра сезон в 16:30. После их встречи создай pull request на GitHub и отправь ссылку в Slack. "
        "Please review the changes before merging into main. Бюджет — 12 долларов 50 центов. "
        "Документы пока не отправляй. Сначала дождись нового подтверждения."
    )
    dictator = _dictator(corrected)
    assert dictator.clean(raw, "default") == corrected
    assert dictator.llm.requests[0][1] == raw


@pytest.mark.parametrize(
    ("raw", "corrected"),
    [
        ("Не отправляй документы, нет, отправляй.", "Отправляй документы."),
        ("Проверь почту. Ну. Ответь коллегам.", "Проверь почту и ответь коллегам."),
        ("коротко", "Длинный ответ. " * 20),
    ],
)
def test_cleanup_accepts_successful_llm_rewrites(raw, corrected):
    assert _dictator(corrected).clean(raw, "default") == corrected


@pytest.mark.parametrize("reply", [TimeoutError("ollama is slow"), ""])
def test_failed_or_empty_cleanup_falls_back_to_unmodified_raw_text(reply):
    raw = "Завтра в 15:30. Нет, перенеси на 16 30. Документы пока не отправляй"
    dictator = _dictator(reply)
    assert dictator.clean(raw, "default") == raw
    assert dictator.llm.requests[0][1] == raw


def test_cloud_cleanup_failure_keeps_raw_but_translation_failure_raises(monkeypatch):
    from wav2sum import dictation

    class CloudLLM(ScriptedLLM):
        def __init__(self, **kwargs):
            super().__init__(RuntimeError("cloud unavailable"))

    monkeypatch.setattr(dictation, "OpenAILLM", CloudLLM)
    dictator = Dictator(SimpleNamespace(cloud=True), DictationConfig())
    raw = "ну давай созвонимся в пятницу"
    assert dictator.clean(raw, "default") == raw
    with pytest.raises(RuntimeError, match="cloud unavailable"):
        dictator.translate(raw, "default")


def test_cleanup_off_inserts_raw_text():
    dictator = _dictator(AssertionError("LLM must not run"), cleanup=False)
    assert dictator.clean("ну давай созвонимся в пятницу", "email") == "ну давай созвонимся в пятницу"
    assert dictator.llm.requests == []


def test_voice_command_rewrites_the_selection():
    dictator = _dictator("Short text")
    dictator.models = fake_models("en")
    dictation = dictator.dictate(np.zeros(16_000), selection="A very long text", command=True)
    assert (dictation.text, dictation.command, dictation.language) == ("Short text", True, "ru")
    [(system, user)] = dictator.llm.requests
    assert system.startswith("Ты — помощник по работе с текстом")
    assert user == "Текст:\nA very long text\n\nКоманда: hello"


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
    dictator.models.dictation_asr = FakeAsr("ну давай созвонимся")
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
    dictator = _dictator("Hello.")
    dictator.models = fake_models("en")
    dictator.language = "en"
    assert dictator.dictate(np.zeros(16_000), translate=True).text == "Hello."
    assert dictator.llm.requests[0][1] == "hello"


@pytest.mark.parametrize(("language", "expected"), [("ru", "Hi"), ("en", "hello")])
def test_auto_translation_uses_final_text_language(language, expected):
    dictator = _dictator("Hi", cleanup=False)
    dictator.models = fake_models(language)
    dictator.language = AUTO
    result = dictator.dictate(np.zeros(16_000), translate=True)
    assert (result.language, result.text) == (language, expected)
    assert len(dictator.llm.requests) == (1 if language == "ru" else 0)


def test_broken_or_runaway_translation_raises():
    raw = "ну давай созвонимся в пятницу"
    with pytest.raises(TimeoutError):
        _dictator(TimeoutError("ollama is slow")).translate(raw, "default")
    with pytest.raises(ValueError):
        _dictator("").translate(raw, "default")
    with pytest.raises(ValueError):
        _dictator("Sure! Here is the email: " + "bla " * 50).translate(raw, "default")


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Thanks for the feedback, I will update the docs.", "en"),
        ("Шар The Figh Malink & Swag", "en"),
        ("Проверь, что Python Script работает на Макос.", "ru"),
        ("Фронтентный реактор Bacan Fostape.", "ru"),
        ("", "ru"),
        ("123", "ru"),
    ],
)
def test_text_language_by_the_share_of_latin_letters(text, language):
    assert text_language(text) == language


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
