import pytest

from wav2sum.model_benchmark import error_rate, summarize_timings


def test_word_and_character_error_rate():
    assert error_rate("one two three", "one four three", words=True) == 1 / 3
    assert error_rate("abc", "adc", words=False) == 1 / 3
    assert error_rate("", "", words=True) == 0
    assert error_rate("", "new", words=True) is None


def test_timing_summary_requires_repeats_and_uses_nearest_rank_p95():
    assert summarize_timings([4.0, 1.0, 2.0, 3.0]) == {"samples": [4.0, 1.0, 2.0, 3.0], "median": 2.5, "p95": 4.0}
    with pytest.raises(ValueError, match="three"):
        summarize_timings([1.0, 2.0])
