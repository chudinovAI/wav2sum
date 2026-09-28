from wav2sum.conversation import Piece, Utterance, build_conversation, drop_echo, split_by_turns
from wav2sum.diarizer import Turn
from wav2sum.vad import merge_close_spans


def test_split_by_turns_cuts_span_at_speaker_change():
    turns = [Turn(0, 0.0, 4.0), Turn(1, 4.5, 9.0)]
    assert split_by_turns([(0.2, 8.8)], turns) == [Piece(0.2, 4.25, 0), Piece(4.25, 8.8, 1)]


def test_split_by_turns_absorbs_slivers():
    turns = [Turn(0, 0.0, 3.0), Turn(1, 3.0, 3.2), Turn(0, 3.2, 6.0)]
    assert split_by_turns([(0.0, 6.0)], turns) == [Piece(0.0, 6.0, 0)]


def test_split_by_turns_uses_nearest_speaker_outside_turns():
    turns = [Turn(1, 10.0, 12.0)]
    assert split_by_turns([(0.0, 1.0)], turns) == [Piece(0.0, 1.0, 1)]
    assert split_by_turns([(0.0, 1.0)], []) == [Piece(0.0, 1.0, None)]


def test_drop_echo_removes_leaked_copy_only():
    theirs = [Utterance(10.0, 14.0, "Бэкенд почти готов, нужно ещё два дня.", "Илья")]
    mine = [
        Utterance(10.1, 13.9, "бэкенд почти готов нужно ещё два", "Я"),
        Utterance(14.5, 17.0, "Понял, тогда переносим релиз на среду.", "Я"),
    ]
    kept = drop_echo(mine, theirs)
    assert [u.text for u in kept] == ["Понял, тогда переносим релиз на среду."]


def test_drop_echo_keeps_same_words_at_other_times():
    theirs = [Utterance(10.0, 11.0, "Да, согласен.", "Илья")]
    mine = [Utterance(60.0, 61.0, "Да, согласен.", "Я")]
    assert drop_echo(mine, theirs) == mine


def test_build_conversation_orders_and_merges():
    utts = [
        Utterance(2.0, 3.0, "Привет.", "Илья"),
        Utterance(0.0, 1.0, "Привет!", "Я"),
        Utterance(3.5, 4.5, "Как дела?", "Я"),
        Utterance(5.0, 6.0, "Слышишь?", "Я"),
        Utterance(20.0, 21.0, "Алло?", "Я"),
    ]
    conv = build_conversation(utts)
    assert [(u.speaker, u.text) for u in conv] == [
        ("Я", "Привет!"),
        ("Илья", "Привет."),
        ("Я", "Как дела? Слышишь?"),
        ("Я", "Алло?"),
    ]


def test_merge_close_spans_respects_gap_and_max_len():
    spans = [(0.0, 1.0), (1.3, 2.0), (3.0, 4.0), (4.2, 25.0)]
    assert merge_close_spans(spans, max_gap=0.6, max_len=20.0) == [(0.0, 2.0), (3.0, 4.0), (4.2, 25.0)]
