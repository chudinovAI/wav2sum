from wav2sum.llm import LLM
from wav2sum.summarizer import chunk_transcript, summarize


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
