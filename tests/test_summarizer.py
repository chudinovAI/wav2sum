from wav2sum.llm import LLM
from wav2sum.summarizer import chapters, chunk_transcript, summarize


class FakeLLM(LLM):
    def __init__(self, num_ctx: int = 16384):
        super().__init__("fake", num_ctx=num_ctx)
        self.prompts: list[str] = []

    def chat(self, system: str, user: str, examples=()) -> str:
        self.prompts.append(system)
        return "заметки"

    def count_tokens(self, text: str) -> int:
        return len(text.split())


def test_chunks_keep_utterances_whole_and_in_order():
    utterances = [f"Спикер {i % 2}: " + "слово " * 20 for i in range(10)]
    transcript = "\n\n".join(utterances)
    chunks = chunk_transcript(transcript, 300, len)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    assert "\n\n".join(chunks) == transcript


def test_terms_reach_every_prompt_of_a_long_call():
    llm = FakeLLM(num_ctx=4096 + 100)
    summarize("\n\n".join(["Я: " + "слово " * 30] * 10), llm, ["Kubernetes", "GigaAM"])
    assert len(llm.prompts) > 2
    assert all("Kubernetes, GigaAM" in p for p in llm.prompts)


def test_no_glossary_without_terms():
    llm = FakeLLM()
    summarize("Я: привет", llm)
    assert "Термины" not in llm.prompts[0]


def test_english_call_gets_english_prompts():
    llm = FakeLLM()
    summarize("Me: hi", llm, ["GigaAM"], language="en")
    assert llm.prompts[0].startswith("You write a summary")
    assert "spell them exactly like this: GigaAM." in llm.prompts[0]


def test_long_call_is_summarized_from_notes_on_its_parts():
    class Recording(FakeLLM):
        def chat(self, system, user, examples=()):
            self.prompts.append(system)
            self.users = [*getattr(self, "users", []), user]
            return f"заметка {len(self.prompts)}"

    llm = Recording(num_ctx=4096 + 100)
    assert summarize("\n\n".join(["Я: " + "слово " * 30] * 10), llm) == f"заметка {len(llm.prompts)}"
    *parts, final = llm.users
    assert all(p.startswith("Я: ") for p in parts)
    assert final.startswith("Заметки по частям созвона:\n\n--- Часть 1 ---\nзаметка 1")
    assert all("Это только ЧАСТЬ встречи" in p for p in llm.prompts[:-1])
    assert "Это только ЧАСТЬ встречи" not in llm.prompts[-1]


def test_an_utterance_longer_than_a_chunk_is_kept_whole():
    long = "Я: " + "слово " * 100
    assert chunk_transcript(f"Я: да\n\n{long}\n\nОн: нет", 50, len) == ["Я: да", long, "Он: нет"]


def test_a_call_that_fits_in_tokens_goes_in_one_pass_whatever_its_length_in_chars():
    llm = FakeLLM(num_ctx=4096 + 1000)
    summarize("Я: " + "антидисэстаблишментарианство " * 300, llm)
    assert len(llm.prompts) == 1


def test_the_system_prompt_with_its_glossary_counts_against_the_budget():
    transcript = "\n\n".join(["Я: " + "слово " * 30] * 10)
    llm = FakeLLM(num_ctx=4096 + 1000)
    summarize(transcript, llm)
    assert len(llm.prompts) == 1

    llm = FakeLLM(num_ctx=4096 + 1000)
    summarize(transcript, llm, [f"термин{i}" for i in range(500)])
    assert len(llm.prompts) > 1


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


def test_chapters_keep_only_lines_at_utterance_starts_and_run_without_thinking():
    llm = ChaptersLLM("Вот главы:\n- \n- 00:01 — Знакомство\n- 00:30 — Выдумка\n- 01:25 — GPU\n- 1:02:03 — Прощание")
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
    assert chapters(TRANSCRIPT, ChaptersLLM("- 00:30 — Выдумка")) == ""

    class Looping(ChaptersLLM):
        def chat(self, *args, **kwargs):
            raise ValueError("LLM reply hit the 100-token limit")

    assert chapters(TRANSCRIPT, Looping("")) == ""


def test_a_long_call_gets_chapters_from_each_part():
    transcript = "\n\n".join(f"[{i:02d}:00 – {i:02d}:30] Я: " + "слово " * 30 for i in range(10))
    llm = ChaptersLLM("- 00:00 — Начало", num_ctx=4096 + 100)
    assert chapters(transcript, llm) == "**Главы**\n" + "\n".join(["- 00:00 — Начало"] * len(llm.calls))
    assert len(llm.calls) > 1
    assert "\n\n".join(user for _, user, _, _ in llm.calls) == transcript
