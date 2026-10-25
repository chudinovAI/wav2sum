from types import SimpleNamespace

from wav2sum import llm as llm_module
from wav2sum.llm import LLM


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content="  ответ \n"))


def test_chat_sends_system_examples_and_options():
    llm = LLM("gemma", num_ctx=4096, temperature=0.2, think=False, keep_alive="10m")
    llm.client = FakeClient()

    assert llm.chat("будь краток", "привет", [("ну э привет", "Привет."), ("пока", "Пока.")]) == "ответ"

    [call] = llm.client.calls
    assert call["model"] == "gemma"
    assert [(m["role"], m["content"]) for m in call["messages"]] == [
        ("system", "будь краток"),
        ("user", "ну э привет"),
        ("assistant", "Привет."),
        ("user", "пока"),
        ("assistant", "Пока."),
        ("user", "привет"),
    ]
    assert call["options"] == {"num_ctx": 4096, "temperature": 0.2}
    assert (call["think"], call["keep_alive"]) == (False, "10m")


def test_client_ignores_system_proxy(monkeypatch):
    made = []
    monkeypatch.setattr(llm_module.ollama, "Client", lambda **kwargs: made.append(kwargs))
    LLM("gemma", timeout=30)
    assert made == [{"timeout": 30, "trust_env": False}]
