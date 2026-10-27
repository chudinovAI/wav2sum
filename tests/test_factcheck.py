import json

from wav2sum.factcheck import report, unsupported
from wav2sum.llm import LLM

TRANSCRIPT = """\
[03:09 – 03:23] Собеседник: They're using a hundred thousand GPUs, 50,000 GPUs. Like for example, Astra.

[04:13 – 04:50] Собеседник: Mistral used 3800 GPUs. I can't imagine it would be less than 50 000.

[05:00 – 05:10] Я: Скажи Илье, что 1,5 месяца хватит."""


def test_number_missing_from_the_transcript_is_reported():
    summary = "**Discussion** —\n* Mistral may require 50,000 to 100,000 GPUs.\n* Mistral used 3 800 GPUs."
    assert unsupported(summary, TRANSCRIPT) == [("* Mistral may require 50,000 to 100,000 GPUs.", ["100,000"])]


def test_number_written_another_way_is_found():
    assert unsupported("Нужно 50 000 GPU, 3,800 или 50000, и 1.5 месяца.", TRANSCRIPT) == []


def test_timestamps_are_not_numbers_of_the_call():
    assert unsupported("Решили к 03:09.", TRANSCRIPT) == [("Решили к 03:09.", ["03", "09"])]


def test_name_missing_from_the_transcript_is_reported():
    summary = "* He said Andre compared Astra with Mistral, and Yee works at DeepMind.\n* Решили, что Илья напишет."
    assert unsupported(summary, TRANSCRIPT) == [
        ("* He said Andre compared Astra with Mistral, and Yee works at DeepMind.", ["Andre", "Yee", "DeepMind"])
    ]


def test_headers_and_sentence_starts_are_not_names():
    summary = "**Тема** — Обсудили GPU.\n**Решения** — Не обсуждалось. Поговорили про Astra's GPUs."
    assert unsupported(summary, TRANSCRIPT) == []


class FakeLLM(LLM):
    def __init__(self):
        super().__init__("fake")
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, examples=()) -> str:
        self.calls.append((system, user))
        return "**Topic** — GPUs.\n* He said Yee asked for 100,000 GPUs."

    def count_tokens(self, text: str) -> int:
        return len(text.split())


def test_report_summarizes_each_call_again_in_its_language(tmp_path):
    call = tmp_path / "call-2026-10-20_14-15-56"
    call.mkdir()
    (call / "transcript.txt").write_text(TRANSCRIPT, encoding="utf-8")
    (call / "meta.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
    (call / "summary.md").write_text("old", encoding="utf-8")
    llm = FakeLLM()

    text = report([call], llm, ["Astra"])

    system, user = llm.calls[0]
    assert system.startswith("You write a summary") and "Astra." in system
    assert "[03:09" not in user and "Собеседник: They're using" in user
    assert "## call-2026-10-20_14-15-56" in text
    assert "* He said Yee asked for 100,000 GPUs.\n  not in the transcript: Yee, 100,000" in text
    assert (call / "summary.md").read_text(encoding="utf-8") == "old"
