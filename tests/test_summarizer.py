from wav2sum.llm import LLM
from wav2sum.summarizer import chunk_transcript, summarize


class FakeLLM(LLM):
    def __init__(self, num_ctx: int = 16384):
        super().__init__("fake", num_ctx=num_ctx)
        self.prompts: list[str] = []

    def chat(self, system: str, user: str, examples=()) -> str:
        self.prompts.append(system)
        return "заметки"


def test_chunks_keep_utterances_whole_and_in_order():
    utterances = [f"Спикер {i % 2}: " + "слово " * 20 for i in range(10)]
    transcript = "\n\n".join(utterances)
    chunks = chunk_transcript(transcript, max_chars=300)
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
