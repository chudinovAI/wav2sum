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


def test_learns_names_and_case_fixes():
    assert corrections("Передай милене, что всё готово", "Передай Милене, что всё готово") == [("милене", "Милене")]
    assert corrections("Залей на гитхаб", "Залей на GitHub") == [("гитхаб", "GitHub")]


def test_vocabulary_persists_and_fixes_new_text(tmp_path):
    path = tmp_path / "vocabulary.json"
    Vocabulary(path).learn("Запусти гигаам на маке", "Запусти GigaAM на маке")

    vocabulary = Vocabulary(path)
    assert vocabulary.words == ["GigaAM"]
    assert vocabulary.apply("Гигаам и гигаамы, гигаам.") == "GigaAM и гигаамы, GigaAM."
