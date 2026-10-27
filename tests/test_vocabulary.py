from wav2sum.vocabulary import Vocabulary, corrections


def test_learns_word_fixes():
    original = "Нужно перенести сервис в Кьюбернидс до пятницы, скажи Гите."
    edited = "Нужно перенести сервис в Kubernetes до пятницы, скажи Гите."
    assert corrections(original, edited) == [("Кьюбернидс", "Kubernetes")]


def test_learns_phrase_fixes():
    assert corrections("Открой пулл реквест в гит хабе", "Открой pull request в GitHub") == [
        ("пулл реквест", "pull request"),
        ("гит хабе", "GitHub"),
    ]


def test_ignores_rewrites_additions_and_trivial_edits():
    assert corrections("Давай созвонимся в пятницу", "Слушай, давай лучше завтра утром спишемся") == []
    assert corrections("Привет. Как дела?", "Привет. Как дела? Я тут подумал") == []
    assert corrections("привет, как дела", "Привет, как дела") == []
    assert corrections("Встреча в пять, приходи", "Встреча в 17:00, приходи") == []
    assert corrections("Готово", "Готово!") == []
    assert corrections("Готово", "") == []


def test_ignores_ordinary_words_and_grammar():
    assert corrections("Давай завтра созвонимся", "Давай послезавтра созвонимся") == []
    assert corrections("Скажи Гите, что всё готово", "Скажи Гиту, что всё готово") == []
    assert corrections("Завтра созвонимся", "Послезавтра созвонимся") == []


def test_ignores_english_grammar_but_learns_english_terms():
    assert corrections("I think there going home", "I think they're going home") == []
    assert corrections("Send it to their team", "Send it to there team") == []
    assert corrections("Parse the jason file", "Parse the JSON file") == [("jason", "JSON")]


def test_learns_names_and_case_fixes():
    assert corrections("Передай милене, что всё готово", "Передай Милене, что всё готово") == [("милене", "Милене")]
    assert corrections("Залей на гитхаб", "Залей на GitHub") == [("гитхаб", "GitHub")]


def test_vocabulary_persists_and_fixes_new_text(tmp_path):
    path = tmp_path / "vocabulary.json"
    Vocabulary(path).learn("Запусти гигаам на маке", "Запусти GigaAM на маке")

    vocabulary = Vocabulary(path)
    assert vocabulary.words == ["GigaAM"]
    assert vocabulary.apply("Гигаам и гигаамы, гигаам.") == "GigaAM и гигаамы, GigaAM."


def test_unreadable_vocabulary_starts_empty(tmp_path):
    path = tmp_path / "vocabulary.json"
    path.write_text("{broken")
    assert Vocabulary(path).pairs == {}


def test_relearning_moves_a_word_to_the_end_and_old_ones_fall_off(monkeypatch):
    monkeypatch.setattr("wav2sum.vocabulary.MAX_PAIRS", 2)
    vocabulary = Vocabulary()
    vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    vocabulary.learn("Запусти гигаам", "Запусти GigaAM")
    vocabulary.learn("Залей на гитхаб", "Залей на GitHub")
    vocabulary.learn("Подними кубернетис", "Подними Kubernetes")
    assert list(vocabulary.pairs) == ["гитхаб", "кубернетис"]


def test_apply_only_replaces_whole_words():
    vocabulary = Vocabulary()
    vocabulary.pairs = {"гит хаб": "GitHub"}
    assert vocabulary.apply("Гит хаб, гит хабу и мой гит хаб.") == "GitHub, гит хабу и мой GitHub."


def test_apply_matches_abbreviations_exactly():
    vocabulary = Vocabulary()
    vocabulary.pairs = {"CD": "CI/CD", "jason": "JSON"}
    assert vocabulary.apply("Настрой CD, потом cd в папку") == "Настрой CI/CD, потом cd в папку"
    assert vocabulary.apply("Jason и jason") == "JSON и JSON"
