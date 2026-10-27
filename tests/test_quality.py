import json

from wav2sum.quality import main, report


def _entry(raw, text, *, time="10:00:00", seconds=3.0, language="ru", **extra):
    return {
        "time": f"2026-10-27T{time}+03:00",
        "seconds": seconds,
        "raw": raw,
        "text": text,
        "language": language,
        "command": False,
        "translate": False,
        **extra,
    }


def test_raw_text_share_counts_only_cleanup_of_more_than_two_words():
    entries = [
        _entry("раз два три", "раз два три"),
        _entry("раз два три четыре", " раз два три четыре"),  # fit_to_cursor may add a space before
        _entry("раз два три", "Раз, два, три."),
        _entry("раз два", "раз два"),  # 1–2 words skip the LLM
        _entry("раз два три", "раз два три", command=True),
        _entry("раз два три", "one two three", translate=True),
    ]
    assert "Raw text inserted: 2 of 3 (67%)" in report(entries)


def test_words_kept_and_strongest_cuts_first():
    entries = [
        _entry("раз два три четыре", "раз два три четыре"),
        _entry("раз два три четыре", "раз два"),
        _entry("раз два три четыре пять шесть семь восемь", "раз два"),
    ]
    text = report(entries)
    assert "Words kept, raw → text: 50% in total, median 50%" in text
    assert text.index("| 25% |") < text.index("| 50% |") < text.index("| 100% |")


def test_english_cyrillic_share_and_short_entries():
    entries = [
        _entry("one two three", "One, two, three.", language="en", seconds=8.0),
        _entry("ну вот так", "ну вот так", language="en", seconds=1.5),
        _entry("call Маша", "Call Маша.", language="en", seconds=2.0),
        _entry("раз два три", "раз два три"),
    ]
    text = report(entries)
    assert "English entries: 3, with Cyrillic in raw: 2, in text: 2, shorter than 5 s: 2" in text
    assert "| 1.5 | 100% | 100% | ну вот так |" in text
    assert "| 2.0 | 50% | 50% | call Маша |" in text
    assert "| 8.0 |" not in text


def test_long_text_is_clipped_and_pipes_escaped():
    entries = [_entry("a|b " + "слово " * 40, "a|b")]
    text = report(entries)
    assert "a\\|b" in text
    assert "…" in text


def test_empty_history():
    assert "No dictations." in report([])


def test_main_leaves_the_file(tmp_path, capsys):
    path = tmp_path / "dictations.jsonl"
    entries = [_entry("раз два три", "раз два три"), _entry("раз два три", "Раз, два, три.")]
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries) + "\n", encoding="utf-8")
    before = path.read_bytes()

    main([str(path)])

    assert "Raw text inserted: 1 of 2 (50%)" in capsys.readouterr().out
    assert path.read_bytes() == before
