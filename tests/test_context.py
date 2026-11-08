from wav2sum.context import canonicalize_terms, recognition_terms


def test_context_is_stable_trimmed_deduplicated_and_rejects_controls():
    assert recognition_terms(
        [" GitHub ", "gitHub", "", "line\nbreak", "a\tb", "\x00bad", "Kubernetes"],
        ["GITHUB", "Qwen", " Kubernetes ", "zero\u200bwidth", "line\u2028break"],
    ) == ("GitHub", "Kubernetes", "Qwen")


def test_context_limits_count_and_utf8_bytes_without_partial_terms():
    assert recognition_terms([f"word{i}" for i in range(60)]) == tuple(f"word{i}" for i in range(50))
    result = recognition_terms(["я" * 248, "too long", "a", "b"])
    assert result == ("я" * 248, "a")
    assert len(", ".join(result).encode("utf-8")) <= 500
    assert recognition_terms(["я" * 251, "valid"]) == ("valid",)


def test_mixed_script_tokens_only_use_exact_transliteration_of_an_existing_term():
    assert canonicalize_terms("Проверяем Немotron и ГитHub.", ("Nemotron", "GitHub")) == "Проверяем Nemotron и GitHub."
    assert canonicalize_terms("Аниматрон и аниматроник.", ("Nemotron",)) == "Аниматрон и аниматроник."
    assert canonicalize_terms("Немotron", ("GitHub",)) == "Немotron"
    assert canonicalize_terms("Немotron", ()) == "Немotron"
    assert canonicalize_terms("Гитхаб", ("GitHub",)) == "Гитхаб"
    assert canonicalize_terms("Нова-3", ("Nova-3",)) == "Нова-3"
