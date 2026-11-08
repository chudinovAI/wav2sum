import re

import pytest

from wav2sum.language import EN, RU
from wav2sum.llm import LLM
from wav2sum.summarizer import chapters, chunk_transcript, summarize


class FakeLLM(LLM):
    def __init__(self, num_ctx: int = 16384):
        super().__init__("fake", num_ctx=num_ctx)
        self.prompts: list[str] = []

    def chat(self, system: str, user: str, examples=(), *, max_growth=None) -> str:
        self.prompts.append(system)
        refs = re.findall(r"\[U\d+ \d+:\d\d(?::\d\d)?–\d+:\d\d(?::\d\d)?\]", user)
        return f"**Topic** — Discussed. {refs[0]}" if refs else ""

    def count_tokens(self, text: str) -> int:
        return len(text.split())


def test_chunks_keep_utterances_whole_and_in_order():
    utterances = [f"Спикер {i % 2}: " + "слово " * 20 for i in range(10)]
    transcript = "\n\n".join(utterances)
    chunks = chunk_transcript(transcript, 300, len)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    assert "\n\n".join(chunks) == transcript


@pytest.mark.parametrize("language", [RU, EN])
def test_summary_has_verified_readable_references_for_repeated_speaker_times(language):
    transcript = "[00:01 – 00:04] Я: Привет\n\n[00:01 – 00:04] Я: Ещё привет"

    class Reply(FakeLLM):
        def chat(self, system, user, examples=(), *, max_growth=None):
            assert "[U1 00:01–00:04]" in user
            assert "[U2 00:01–00:04]" in user
            return "**Topic** — Привет. [U2 00:01–00:04]\n**Discussion**\n- Встреча. [U1 00:01–00:04]"

    assert "[U2 00:01–00:04]" in summarize(transcript, Reply(), language=language)


@pytest.mark.parametrize(
    "reply",
    [
        "**Topic** — No citation.",
        "**Topic** — Fabricated. [U9 00:01–00:04]",
        "**Topic** — Wrong timestamp. [U1 00:01–00:05]",
        "**Topic** — Malformed. [U1]",
        "**Topic** — Mixed. [U1 00:01–00:04] [U8]",
        "**Topic** — Valid. [U1 00:01–00:04]\n- Unsupported point",
        "**Topic** — Valid. [U1 00:01–00:04]\n**Решили перечислить 100 рублей**",
        "**Topic** — Unsupported. <!-- [U1 00:01–00:04] -->",
        "**Topic** — Unsupported. [details](https://example.test/[U1 00:01–00:04])",
        "**Topic** — Valid. [U1 00:01–00:04]\n<!--\n- Hidden [U1 00:01–00:04]\n-->\n- Unsupported.",
        "**Topic** — Valid. [U1 00:01–00:04]\n- Unsupported. [details](https://example.test/[U1 00:01–00:04])",
        "**Topic** — Valid. [U1 00:01–00:04]\n- Invalid hidden reference. <!-- [U9 00:01–00:04] -->",
        "**Topic** — Valid. [U1 00:01–00:04]\n- Invalid hidden marker. <!-- [U9] -->",
        "",
    ],
)
def test_summary_rejects_uncited_or_invalid_references(reply):
    class Reply(FakeLLM):
        def chat(self, system, user, examples=(), *, max_growth=None):
            return reply

    with pytest.raises(ValueError):
        summarize("[00:01 – 00:04] Я: Привет", Reply())


def test_empty_transcript_does_not_call_model_and_empty_heading_needs_no_reference():
    llm = FakeLLM()
    assert summarize("", llm) == ""
    assert not llm.prompts

    class Reply(FakeLLM):
        def chat(self, system, user, examples=(), *, max_growth=None):
            return "**Topic** — Привет. [U1 00:01–00:04]\n**Discussion**"

    assert summarize("[00:01 – 00:04] Я: Привет", Reply()).endswith("**Discussion**")


def test_custom_meeting_heading_is_grounded():
    class Reply(FakeLLM):
        def chat(self, system, user, examples=(), *, max_growth=None):
            return "**Topic** — Урок. [U1 00:01–00:04]\n**Советы преподавателя** [U1 00:01–00:04]"

    assert "**Советы преподавателя** [U1 00:01–00:04]" in summarize(
        "[00:01 – 00:04] Я: Советую практиковаться", Reply()
    )


def test_untimestamped_or_malformed_transcript_is_rejected():
    for transcript in ("Я: привет", "[00:99 – 01:00] Я: привет", "[00:02 – 00:01] Я: привет"):
        with pytest.raises(ValueError):
            summarize(transcript, FakeLLM())


def test_out_of_chunk_reference_is_rejected():
    transcript = "\n\n".join(f"[{i:02d}:00 – {i:02d}:01] Я: " + "слово " * 30 for i in range(20))

    class WrongChunk(FakeLLM):
        def chat(self, system, user, examples=(), *, max_growth=None):
            return "- Point. [U20 19:00–19:01]"

    with pytest.raises(ValueError):
        summarize(transcript, WrongChunk(num_ctx=4096 + 400))


def test_reduction_cannot_cite_a_reference_from_another_batch():
    transcript = "\n\n".join(f"[{i:02d}:00 – {i:02d}:01] Я: " + "слово " * 70 for i in range(50))

    class WrongReduction(FakeLLM):
        def chat(self, system, user, examples=(), *, max_growth=None):
            refs = re.findall(r"\[U\d+ \d+:\d\d–\d+:\d\d\]", user)
            ref = "[U1 00:00–00:01]" if "Заметки по частям" in user else refs[0]
            return "- " + "обсудили " * 25 + ref

    with pytest.raises(ValueError, match="out-of-chunk"):
        summarize(transcript, WrongReduction(num_ctx=4096 + 430))


def test_reduction_that_does_not_shrink_fails():
    transcript = "\n\n".join(f"[{i:02d}:00 – {i:02d}:01] Я: " + "слово " * 70 for i in range(20))

    class Verbose(FakeLLM):
        def chat(self, system, user, examples=(), *, max_growth=None):
            ref = re.search(r"\[U\d+ \d+:\d\d–\d+:\d\d\]", user).group()
            return "- " + "обсудили " * 160 + ref

    with pytest.raises(ValueError, match="could not be reduced"):
        summarize(transcript, Verbose(num_ctx=4096 + 430))


def test_recursive_notes_stay_within_context_and_preserve_original_citations():
    transcript = "\n\n".join(f"[{i:02d}:00 – {i:02d}:01] Я: " + "слово " * 70 for i in range(50))

    class Reducing(FakeLLM):
        def __init__(self):
            super().__init__(num_ctx=4096 + 430)
            self.users = []

        def chat(self, system, user, examples=(), *, max_growth=None):
            assert self.count_tokens(system) + self.count_tokens(user) <= self.num_ctx - 4096
            assert int(max_growth * self.count_tokens(user)) + 64 <= 4096
            self.users.append(user)
            refs = list(dict.fromkeys(re.findall(r"\[U\d+ \d+:\d\d–\d+:\d\d\]", user)))
            return "- " + "обсудили " * 25 + " ".join(dict.fromkeys((refs[0], refs[-1])))

    llm = Reducing()
    result = summarize(transcript, llm)
    assert "[U1 00:00–00:01]" in result
    assert "[U50 49:00–49:01]" in result
    assert len(llm.users) > 2
    assert any(user.count("- обсудили") > 1 for user in llm.users)


def test_long_single_utterance_is_fragmented_and_summarized_with_its_original_reference():
    words = [f"фрагмент{i}" for i in range(7000)]
    transcript = "[00:00 – 12:34] Я: " + " ".join(words)

    class Reducing(FakeLLM):
        def __init__(self):
            super().__init__(num_ctx=4096 + 430)
            self.users = []

        def chat(self, system, user, examples=(), *, max_growth=None):
            assert self.count_tokens(system) + self.count_tokens(user) <= self.num_ctx - 4096
            assert int(max_growth * self.count_tokens(user)) + 64 <= 4096
            self.users.append(user)
            assert set(re.findall(r"\[U\d+ \d+:\d\d–\d+:\d\d\]", user)) == {"[U1 00:00–12:34]"}
            return "- " + "обсудили " * 25 + "[U1 00:00–12:34]"

    llm = Reducing()
    result = summarize(transcript, llm)
    assert "[U1 00:00–12:34]" in result
    map_users = [user for user in llm.users if user.startswith("Транскрипт созвона:")]
    assert len(map_users) > 1
    assert [word for user in map_users for word in re.findall(r"фрагмент\d+", user)] == words
    assert any(user.count("- обсудили") > 1 for user in llm.users)


def test_single_utterance_without_whitespace_is_split_without_losing_text():
    content = "я" * 20000

    class CharacterCountingLLM(FakeLLM):
        def __init__(self):
            super().__init__(num_ctx=4096 + 6000)
            self.users = []

        def count_tokens(self, text):
            return len(text)

        def chat(self, system, user, examples=(), *, max_growth=None):
            assert self.count_tokens(system) + self.count_tokens(user) <= self.num_ctx - 4096
            self.users.append(user)
            return "- Факт. [U1 00:00–01:00]"

    llm = CharacterCountingLLM()
    assert "[U1 00:00–01:00]" in summarize("[00:00 – 01:00] Я: " + content, llm)
    fragments = [
        user.split("[U1 00:00–01:00] Я: ", 1)[1] for user in llm.users if user.startswith("Транскрипт созвона:")
    ]
    assert len(fragments) > 1
    assert "".join(fragments) == content


def test_impossible_prompt_budget_is_rejected():
    with pytest.raises(ValueError):
        summarize("[00:00 – 00:01] Я: слово", FakeLLM(num_ctx=4096 + 1))


def test_an_utterance_longer_than_a_chunk_is_kept_whole():
    long = "Я: " + "слово " * 100
    assert chunk_transcript(f"Я: да\n\n{long}\n\nОн: нет", 50, len) == ["Я: да", long, "Он: нет"]


def test_chunks_fit_the_token_budget():
    utterances = [f"Спикер {i % 2}: " + "слово " * (i + 1) for i in range(20)]
    chunks = chunk_transcript("\n\n".join(utterances), 40, lambda text: len(text.split()))
    assert len(chunks) > 1
    assert all(len(c.split()) <= 40 for c in chunks)


class ChaptersLLM(FakeLLM):
    def __init__(self, reply, num_ctx=16384):
        super().__init__(num_ctx)
        self.reply = reply
        self.calls = []

    def chat(self, system, user, examples=(), max_growth=None, think=None):
        self.calls.append((system, user, max_growth, think))
        return self.reply


TRANSCRIPT = "[00:01 – 00:04] Я: Привет\n\n[01:25 – 01:30] Илья: Про GPU\n\n[1:02:03 – 1:02:09] Я: Пока"


def test_chapters_snap_to_utterance_starts_and_run_without_thinking():
    llm = ChaptersLLM("Вот главы:\n- \n- 00:00 — Знакомство\n- 00:30 — Выдумка\n- 01:27 — GPU\n- 1:02:03 — Прощание")
    assert chapters(TRANSCRIPT, llm) == "**Главы**\n- 00:01 — Знакомство\n- 01:25 — GPU\n- 1:02:03 — Прощание"
    [(system, user, max_growth, think)] = llm.calls
    assert system.startswith("Тебе дан транскрипт созвона с таймкодами")
    assert user == TRANSCRIPT
    assert think is False
    assert max_growth is not None


def test_english_chapters_get_an_english_prompt_and_title():
    llm = ChaptersLLM("- 00:01 — Intro")
    assert chapters(TRANSCRIPT, llm, language="en") == "**Chapters**\n- 00:01 — Intro"
    assert llm.calls[0][0].startswith("You are given a call transcript with timestamps")


def test_no_chapters_without_a_valid_line_or_when_the_reply_runs_away():
    assert chapters(TRANSCRIPT, ChaptersLLM("- 00:30 — Выдумка\n- 00:07 — Тоже мимо")) == ""
    assert chapters("", ChaptersLLM("- 00:00 — Начало")) == ""

    class Looping(ChaptersLLM):
        def chat(self, *args, **kwargs):
            raise ValueError("LLM reply hit the 100-token limit")

    assert chapters(TRANSCRIPT, Looping("")) == ""


@pytest.mark.parametrize("fail_at", [1, 2])
def test_optional_chapters_provider_failure_is_not_retried(fail_at):
    class ProviderFailure(ChaptersLLM):
        def chat(self, system, user, examples=(), max_growth=None, think=None):
            self.calls.append((system, user, max_growth, think))
            if len(self.calls) == fail_at:
                raise RuntimeError("Cloud provider request failed")
            return "\n".join(["- 00:01 — Intro"] * 9)

    llm = ProviderFailure("")
    assert chapters(TRANSCRIPT, llm) == ""
    assert len(llm.calls) == fail_at


def test_a_long_call_gets_chapters_from_each_part():
    transcript = "\n\n".join(f"[{i:02d}:00 – {i:02d}:30] Я: " + "слово " * 30 for i in range(10))
    llm = ChaptersLLM("- 00:00 — Начало", num_ctx=4096 + 150)
    assert chapters(transcript, llm) == "**Главы**\n" + "\n".join(["- 00:00 — Начало"] * len(llm.calls))
    assert 1 < len(llm.calls) <= 8
    assert "\n\n".join(user for _, user, _, _ in llm.calls) == transcript


def test_more_than_8_chapters_are_merged_in_one_more_call():
    transcript = "\n\n".join(f"[{i:02d}:00 – {i:02d}:30] Я: тема {i}" for i in range(12))

    class Merging(ChaptersLLM):
        def chat(self, system, user, examples=(), max_growth=None, think=None):
            self.calls.append((system, user, max_growth, think))
            if "Объедини" in system:
                return "- 00:00 — Первая половина\n- 06:00 — Вторая половина"
            return "\n".join(f"- {i:02d}:00 — Тема {i}" for i in range(12))

    llm = Merging("")
    assert chapters(transcript, llm) == "**Главы**\n- 00:00 — Первая половина\n- 06:00 — Вторая половина"
    assert llm.calls[1][1] == "\n".join(f"- {i:02d}:00 — Тема {i}" for i in range(12))
    assert llm.calls[1][3] is False
