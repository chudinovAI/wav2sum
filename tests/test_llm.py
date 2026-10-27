import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from wav2sum import llm as llm_module
from wav2sum.llm import LLM


class FakeTokenizer:
    has_thinking = True
    think_end = "<channel|>"

    def apply_chat_template(self, messages, add_generation_prompt, enable_thinking):
        assert add_generation_prompt
        return {"messages": messages, "thinking": enable_thinking}

    def encode(self, text):
        return text.split()


class FakeMLX:
    """Stands in for mlx_lm and mlx: records loads and generations."""

    def __init__(self, reply="<|channel>thought долго думал<channel|>  ответ \n", seconds=0.0):
        self.reply, self.seconds = reply, seconds
        self.gate = None
        self.error = None
        self.looping = False
        self.hold = None  # (model, "prefill" or "tokens", event): its next generation stalls there until the event
        self.loads, self.calls = [], []
        self.running = self.most_running = 0

    def load(self, name):
        self.loads.append(name)
        weights = {"w": SimpleNamespace(nbytes=3 * 2**20)}
        return SimpleNamespace(name=name, parameters=lambda: weights), FakeTokenizer()

    def stream_generate(self, model, tokenizer, prompt, *, max_tokens, sampler, prompt_progress_callback):
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        time.sleep(self.seconds)
        if self.gate:
            self.gate.wait()
        if self.error:
            raise self.error
        self.calls.append({"model": model.name, "prompt": prompt, "max_tokens": max_tokens, "temp": sampler})
        _, phase, event = self.hold if self.hold and self.hold[0] == model.name else (None, None, None)
        self.hold = None if phase else self.hold
        while phase == "prefill" and not event.is_set():
            prompt_progress_callback(1, 2)
            time.sleep(0.01)
        reply = " ".join(["бла"] * max_tokens) if self.looping else self.reply
        for i, char in enumerate(reply):
            while phase == "tokens" and i == 1 and not event.is_set():
                yield SimpleNamespace(text="")
                time.sleep(0.01)
            yield SimpleNamespace(text=char)
        self.running -= 1


TIMERS = []


class FakeTimer:
    """Fires only when the test says so, so idle unloads do not depend on the runner's speed."""

    def __init__(self, seconds, fire):
        self.seconds, self.fire, self.cancelled = seconds, fire, False
        TIMERS.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def _idle_timeout_passes():
    [timer] = [t for t in TIMERS if not t.cancelled]
    timer.fire()
    llm_module._worker.submit(lambda: None).result()


@pytest.fixture
def mlx(monkeypatch):
    fake = FakeMLX()
    TIMERS.clear()
    monkeypatch.setattr(llm_module.threading, "Timer", FakeTimer)
    mlx_lm = ModuleType("mlx_lm")
    mlx_lm.load, mlx_lm.stream_generate = fake.load, fake.stream_generate
    sample_utils = ModuleType("mlx_lm.sample_utils")
    sample_utils.make_sampler = lambda temp: temp
    core = ModuleType("mlx.core")
    core.eval = core.clear_cache = lambda *_: None
    utils = ModuleType("mlx.utils")
    utils.tree_flatten = lambda tree: list(tree.items())
    mlx = ModuleType("mlx")
    mlx.core, mlx.utils = core, utils
    for name, module in {
        "mlx_lm": mlx_lm,
        "mlx_lm.sample_utils": sample_utils,
        "mlx": mlx,
        "mlx.core": core,
        "mlx.utils": utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    yield fake
    for name in list(llm_module._loaded):
        llm_module._unload(name)


def test_chat_sends_system_examples_and_options_and_drops_the_thinking(mlx):
    llm = LLM("gemma", num_ctx=4096, temperature=0.2, think=False)

    assert llm.chat("будь краток", "привет", [("ну э привет", "Привет."), ("пока", "Пока.")]) == "ответ"

    [call] = mlx.calls
    assert [(m["role"], m["content"]) for m in call["prompt"]["messages"]] == [
        ("system", "будь краток"),
        ("user", "ну э привет"),
        ("assistant", "Привет."),
        ("user", "пока"),
        ("assistant", "Пока."),
        ("user", "привет"),
    ]
    assert (call["prompt"]["thinking"], call["max_tokens"], call["temp"]) == (False, 4096, 0.2)


def test_reply_is_limited_by_the_input_length_and_a_runaway_raises(mlx):
    llm = LLM("gemma", num_ctx=4096)
    assert llm.chat("s", "раз два три четыре", max_growth=1.5) == "ответ"
    mlx.looping = True
    with pytest.raises(ValueError):
        llm.chat("s", "раз два три четыре", max_growth=1.5)
    assert [call["max_tokens"] for call in mlx.calls] == [70, 70]


def test_think_turns_on_thinking(mlx):
    LLM("gemma", think=True).chat("s", "u")
    assert mlx.calls[0]["prompt"]["thinking"] is True


def test_reply_without_thinking_is_kept(mlx):
    mlx.reply = " просто ответ "
    assert LLM("gemma").chat("s", "u") == "просто ответ"


def test_weights_load_once_and_are_shared_by_the_same_model(mlx):
    LLM("gemma").chat("s", "u")
    LLM("gemma", think=True).chat("s", "u")
    LLM("other").chat("s", "u")
    assert mlx.loads == ["gemma", "other"]
    assert llm_module.memory_mb() == 6


def test_weights_are_freed_after_keep_alive_idle_and_reload_on_the_next_chat(mlx):
    llm = LLM("gemma", keep_alive=600)
    llm.chat("s", "u")
    assert llm_module.memory_mb() == 3
    assert TIMERS[0].seconds == 600

    _idle_timeout_passes()
    assert llm_module.memory_mb() == 0

    llm.chat("s", "u")
    assert mlx.loads == ["gemma", "gemma"]


def test_a_chat_resets_the_idle_timer(mlx):
    llm = LLM("gemma", keep_alive=600)
    for _ in range(3):
        llm.chat("s", "u")
    assert [t.cancelled for t in TIMERS] == [True, True, False]
    assert mlx.loads == ["gemma"]


def test_unload_frees_the_weights(mlx):
    llm = LLM("gemma")
    llm.chat("s", "u")
    llm.unload()
    assert llm_module.memory_mb() == 0


def test_chats_from_two_threads_run_one_at_a_time(mlx):
    mlx.seconds = 0.05
    llm = LLM("gemma")
    threads = [threading.Thread(target=llm.chat, args=("s", "u")) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (len(mlx.calls), mlx.most_running) == (3, 1)


def test_a_chat_that_waits_past_its_timeout_gives_up_and_never_runs(mlx):
    mlx.gate = threading.Event()
    summary = threading.Thread(target=LLM("big").chat, args=("s", "u"))
    summary.start()
    while not mlx.running:
        time.sleep(0.01)
    with pytest.raises(TimeoutError):
        LLM("small", timeout=0.05).chat("s", "u")
    mlx.gate.set()
    summary.join()
    assert mlx.loads == ["big"]


def test_a_failure_after_the_caller_gave_up_is_logged(mlx, caplog):
    mlx.gate, mlx.error = threading.Event(), OSError("error decoding response body")
    with pytest.raises(TimeoutError):
        LLM("gemma", timeout=0.05).chat("s", "u")
    mlx.gate.set()
    llm_module._worker.submit(lambda: None).result()
    assert "LLM failed after the caller stopped waiting" in caplog.text
    assert "error decoding response body" in caplog.text


@pytest.mark.parametrize("phase", ["prefill", "tokens"])
def test_a_dictation_during_a_long_summary_jumps_ahead_and_the_summary_finishes(mlx, phase):
    event = threading.Event()
    mlx.hold = ("big", phase, event)
    summary = []
    thread = threading.Thread(target=lambda: summary.append(LLM("big").chat("s", "u")))
    thread.start()
    while not mlx.calls:
        time.sleep(0.01)
    try:
        assert LLM("small", timeout=1).chat("s", "u") == "ответ"
        assert thread.is_alive()
    finally:
        event.set()
        thread.join()
    assert summary == ["ответ"]
    assert [c["model"] for c in mlx.calls] == ["big", "small"]


def test_a_dictation_inside_a_summary_on_the_same_model_leaves_one_idle_timer(mlx):
    event = threading.Event()
    mlx.hold = ("gemma", "tokens", event)
    thread = threading.Thread(target=LLM("gemma", keep_alive=600).chat, args=("s", "u"))
    thread.start()
    while not mlx.calls:
        time.sleep(0.01)
    LLM("gemma", keep_alive=600, timeout=1).chat("s", "u")
    event.set()
    thread.join()
    assert [t.cancelled for t in TIMERS] == [True, False]
