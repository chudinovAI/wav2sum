import json

from wav2sum.latency import main, report


def _entry(time, *, seconds=3.0, asr=0.4, llm=1.0, language="ru", raw="раз два три", **extra):
    return {
        "time": f"2026-10-27T{time}+03:00",
        "seconds": seconds,
        "raw": raw,
        "language": language,
        "command": False,
        "translate": False,
        "timings": {"asr": asr, "llm": llm},
        **extra,
    }


def _row(text, label):
    return next(line for line in text.splitlines() if line.startswith(f"| {label} |"))


def test_median_and_p90_per_part():
    entries = [_entry(f"10:{i:02}:00", asr=i / 10, llm=i) for i in range(1, 11)]
    assert _row(report(entries), "all") == "| all | 10 | 0.55 | 0.91 | 5.50 | 9.10 | 9% |"


def test_groups_by_length_language_and_mode():
    entries = [
        _entry("10:00:00", seconds=2.0),
        _entry("10:01:00", seconds=7.0, language="en", raw="one two three"),
        _entry("10:02:00", seconds=20.0, translate=True),
        _entry("10:03:00", seconds=40.0, command=True),
        _entry("10:04:00", raw="привет", llm=0.0),
        _entry("10:05:00", language="en", raw="one two three", translate=True),
    ]
    text = report(entries)
    for label, n in [("<5 s", 3), ("5–15 s", 1), ("15–30 s", 1), ("≥30 s", 1), ("ru", 4), ("en", 2)]:
        assert _row(text, label).startswith(f"| {label} | {n} |")
    # Translate only applies to Russian speech; English goes through the usual cleanup.
    for label, n in [("cleanup", 3), ("translate", 1), ("command", 1), ("short", 1)]:
        assert _row(text, label).startswith(f"| {label} | {n} |")


def test_old_entries_without_language_and_rows_in_length_order():
    entries = [_entry("10:00:00", seconds=40.0), _entry("10:01:00", seconds=2.0)]
    del entries[0]["language"]
    text = report(entries)
    assert _row(text, "unknown").startswith("| unknown | 1 |")
    assert text.index("| <5 s |") < text.index("| ≥30 s |")


def test_cold_after_ten_idle_minutes():
    entries = [_entry("10:00:00"), _entry("10:05:00"), _entry("10:15:01"), _entry("10:16:00")]
    text = report(entries)
    assert _row(text, "cold").startswith("| cold | 2 |")
    assert _row(text, "warm").startswith("| warm | 2 |")


def test_main_prints_all_history_and_mlx_only_and_leaves_the_file(tmp_path, capsys):
    path = tmp_path / "dictations.jsonl"
    entries = [_entry("10:00:00") for _ in range(2)]
    entries[0]["time"] = "2026-10-26T22:00:00+03:00"
    del entries[0]["translate"]
    old = {"time": "2026-10-20T10:00:00+03:00", "seconds": 1.0, "raw": "старая", "language": "ru", "command": False}
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in [old, *entries]), encoding="utf-8")
    before = path.read_bytes()

    main([str(path)])

    out = capsys.readouterr().out
    all_history, mlx = out.split("## Since mlx-lm")
    assert _row(all_history, "all").startswith("| all | 2 |")
    assert _row(mlx, "all").startswith("| all | 1 |")
    assert path.read_bytes() == before


def test_pipeline_latency_includes_inflight_work_and_keeps_missing_samples_out():
    entries = [
        _entry(
            f"10:{i:02}:00",
            seconds=2,
            timings={
                "asr": 0.1,
                "llm": 0.2,
                "finalize": i,
                "preview": i / 2,
                "audio_sent": 2,
            },
        )
        for i in range(1, 21)
    ]
    entries.append(_entry("11:00:00"))
    text = report(entries)
    assert _row(text, "cloud final request → ready") == "| cloud final request → ready | 20 | 10.50 | 19.05 |"
    assert _row(text, "cloud first preview").startswith("| cloud first preview | 20 | 5.25 |")
    assert _row(text, "cloud sent / captured") == "| cloud sent / captured | 20 | 1.00 | 1.00 |"


def test_pipeline_metrics_keep_local_cloud_and_zero_length_audio_separate():
    entries = [
        _entry("10:00:00", timings={"asr": 1, "llm": 2, "finalize": 8, "asr_live": 30}),
        _entry("10:01:00", seconds=0, timings={"asr": 1, "llm": 2, "finalize": 4, "audio_sent": 0}),
    ]
    text = report(entries)
    assert _row(text, "local final request → ready") == "| local final request → ready | 1 | 8.00 | 8.00 |"
    assert _row(text, "cloud final request → ready") == "| cloud final request → ready | 1 | 4.00 | 4.00 |"
    assert "| cloud sent / captured |" not in text
    assert "first preview |" not in text


def test_audio_ratio_uses_received_samples_not_rounded_history_duration():
    entry = _entry(
        "10:00:00",
        seconds=0.8,
        timings={
            "asr": 0,
            "llm": 0,
            "audio_sent": 0.75,
            "audio_received": 0.75,
        },
    )
    assert _row(report([entry]), "cloud sent / captured") == "| cloud sent / captured | 1 | 1.00 | 1.00 |"
