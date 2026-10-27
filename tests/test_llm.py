import copy
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from wav2sum import llm as llm_module
from wav2sum.llm import LLM


class FakeTokenizer:
    has_thinking = True
    think_start = "<|channel>thought"
    think_end = "<channel|>"

    def apply_chat_template(self, messages, add_generation_prompt, enable_thinking):
        """One token per message, plus one that opens the reply."""
        tokens = [(m["role"], m["content"]) for m in messages]
        return [*tokens, ("reply", enable_thinking)] if add_generation_prompt else tokens

    def encode(self, text):
        self.thread = threading.current_thread().name
        return text.split()


class FakeCache:
    """The KV cache of one layer: remembers the tokens it has seen; 1 MB each."""

    def __init__(self):
        self.tokens = []

    @property
    def state(self):
        return self.tokens

    @property
    def nbytes(self):
        return len(self.tokens) * 2**20


class FakeModel:
    def __init__(self, name):
        self.name = name
        self.prefills = []
        self.passes = 0
        self.script = []  # prompt and reply tokens: the model predicts the token after each position

    def parameters(self):
        return {"w": SimpleNamespace(nbytes=3 * 2**20)}

    def __call__(self, batch, cache):
        [tokens] = batch
        self.prefills.append(list(tokens))
        self.passes += 1
        start = len(cache[0].tokens)
        cache[0].tokens += tokens
        return [Logits(self.script[start + 1 : start + 1 + len(tokens)])]


class Logits(list):
    """Each position's logits, already decided: the sampler picks the token as is."""

    def __sub__(self, _):
        return self

    def tolist(self):
        return list(self)

    def encode(self, text):
        self.thread = threading.current_thread().name
        return text.split()


class FakeMLX:
    """Stands in for mlx_lm and mlx: records loads and generations."""

    def __init__(self, reply="<|channel>thought долго думал<channel|>  ответ \n", seconds=0.0):
        self.reply, self.seconds = reply, seconds
        self.gate = None
        self.error = None
        self.looping = False
        self.replies = []  # replies for the next generations, before self.reply
        self.hold = None  # (model, "prefill" or "tokens", event): its next generation stalls there until the event
        self.loads, self.calls, self.models = [], [], []
        self.running = self.most_running = 0

    def load(self, name):
        self.loads.append(name)
        self.models.append(FakeModel(name))
        return self.models[-1], FakeTokenizer()

    def stream_generate(
        self, model, tokenizer, prompt, *, max_tokens, sampler, prompt_cache=None, prompt_progress_callback
    ):
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        time.sleep(self.seconds)
        if self.gate:
            self.gate.wait()
        if self.error:
            raise self.error
        self.calls.append(
            {
                "model": model.name,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temp": sampler,
                "cache": copy.deepcopy(prompt_cache),
            }
        )
        if prompt_cache:
            prompt_cache[0].tokens += prompt
        _, phase, event = self.hold if self.hold and self.hold[0] == model.name else (None, None, None)
        self.hold = None if phase else self.hold
        while phase == "prefill" and not event.is_set():
            prompt_progress_callback(1, 2)
            time.sleep(0.01)
        reply = " ".join(["бла"] * max_tokens) if self.looping else self.replies.pop(0) if self.replies else self.reply
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
    models = ModuleType("mlx_lm.models")
    cache = ModuleType("mlx_lm.models.cache")
    cache.make_prompt_cache = lambda model: [FakeCache()]
    cache.KVCache = FakeCache
    cache.trim_prompt_cache = lambda layers, n: [
        layer.tokens.__delitem__(slice(len(layer.tokens) - n, None)) for layer in layers
    ]
    core = ModuleType("mlx.core")
    core.eval = core.clear_cache = lambda *_: None
    core.array = lambda x: x
    core.logsumexp = lambda *_, **__: 0
    utils = ModuleType("mlx.utils")
    utils.tree_flatten = lambda tree: list(tree.items())
    mlx = ModuleType("mlx")
    mlx.core, mlx.utils = core, utils
    for name, module in {
        "mlx_lm": mlx_lm,
        "mlx_lm.sample_utils": sample_utils,
        "mlx_lm.models": models,
        "mlx_lm.models.cache": cache,
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
    assert call["prompt"][:-1] == [
        ("system", "будь краток"),
        ("user", "ну э привет"),
        ("assistant", "Привет."),
        ("user", "пока"),
        ("assistant", "Пока."),
        ("user", "привет"),
    ]
    assert (call["prompt"][-1], call["max_tokens"], call["temp"], call["cache"]) == (("reply", False), 4096, 0.2, None)


def test_reply_is_limited_by_the_input_length_and_a_runaway_raises(mlx):
    llm = LLM("gemma", num_ctx=4096)
    assert llm.chat("s", "раз два три четыре", max_growth=1.5) == "ответ"
    mlx.looping = True
    with pytest.raises(ValueError):
        llm.chat("s", "раз два три четыре", max_growth=1.5)
    assert [call["max_tokens"] for call in mlx.calls] == [70, 70]


def test_tokens_are_counted_by_the_model_tokenizer_in_the_llm_thread(mlx):
    llm = LLM("gemma")
    assert llm.count_tokens("раз два три") == 3
    llm.chat("s", "u")
    assert mlx.loads == ["gemma"]
    assert llm_module._loaded["gemma"][1].thread.startswith("llm")


def test_think_turns_on_thinking(mlx):
    LLM("gemma", think=True).chat("s", "u")
    assert mlx.calls[0]["prompt"][-1] == ("reply", True)


def test_a_call_can_turn_thinking_off(mlx):
    LLM("gemma", think=True).chat("s", "u", think=False)
    assert mlx.calls[0]["prompt"][-1] == ("reply", False)


def test_thinking_that_never_ends_is_dropped_and_the_call_runs_again_without_it(mlx):
    mlx.replies = ["<|channel>thought перебираю заметки, перебираю заметки"]
    mlx.reply = " ответ "
    assert LLM("gemma", think=True).chat("s", "u") == "ответ"
    assert [call["prompt"][-1] for call in mlx.calls] == [("reply", True), ("reply", False)]


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


def test_loaded_tells_whether_the_weights_are_in_memory(mlx):
    llm = LLM("gemma", keep_alive=600)
    assert not llm.loaded
    llm.chat("s", "u")
    assert llm.loaded
    _idle_timeout_passes()
    assert not llm.loaded


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


EXAMPLES = [("ну э привет", "Привет.")]
PREFIX = [("system", "чисти"), ("user", "ну э привет"), ("assistant", "Привет.")]


def test_a_dictation_with_a_known_prefix_only_runs_its_own_message(mlx):
    llm = LLM("gemma", cache_prefix=True)
    assert llm.chat("чисти", "раз", EXAMPLES) == "ответ"
    llm.chat("чисти", "два", EXAMPLES)

    assert mlx.models[0].prefills == [PREFIX]
    assert [c["prompt"] for c in mlx.calls] == [
        [("user", "раз"), ("reply", False)],
        [("user", "два"), ("reply", False)],
    ]
    assert [c["cache"][0].tokens for c in mlx.calls] == [PREFIX, PREFIX]


def test_another_style_or_vocabulary_gets_its_own_prefix(mlx):
    llm = LLM("gemma", cache_prefix=True)
    for system in ("чисти", "чисти для чата", "чисти", "чисти. Слова: GitHub"):
        llm.chat(system, "раз", EXAMPLES)
    assert [p[0][1] for p in mlx.models[0].prefills] == ["чисти", "чисти для чата", "чисти. Слова: GitHub"]


def test_the_least_recently_used_prefix_goes_past_the_limit(mlx):
    llm = LLM("gemma", cache_prefix=True)
    for system in ["0", "1", "2", "3", "0", "4", "0", "1"]:
        llm.chat(system, "раз")
    assert [p[0][1] for p in mlx.models[0].prefills] == ["0", "1", "2", "3", "4", "1"]


def test_prefix_caches_count_as_llm_memory_and_go_with_the_weights(mlx):
    llm = LLM("gemma", keep_alive=600, cache_prefix=True)
    llm.chat("чисти", "раз", EXAMPLES)
    llm.chat("пиши", "раз")
    assert llm_module.memory_mb() == 3 + 3 + 1

    _idle_timeout_passes()
    assert llm_module.memory_mb() == 0

    llm.chat("чисти", "раз", EXAMPLES)
    assert mlx.models[1].prefills == [PREFIX]
    llm.unload()
    assert llm_module.memory_mb() == 0


def test_a_template_that_renders_the_prefix_differently_runs_the_whole_prompt(mlx, monkeypatch):
    render = FakeTokenizer.apply_chat_template
    monkeypatch.setattr(
        FakeTokenizer,
        "apply_chat_template",
        lambda self, messages, add_generation_prompt, enable_thinking: (
            render(self, messages, add_generation_prompt, enable_thinking)
            + ([] if add_generation_prompt else [("end", "")])
        ),
    )
    LLM("gemma", cache_prefix=True).chat("чисти", "раз", EXAMPLES)
    assert mlx.models[0].prefills == []
    assert (len(mlx.calls[0]["prompt"]), mlx.calls[0]["cache"]) == (5, None)


class WordTokenizer(FakeTokenizer):
    """A token per word, so a reply can copy the input."""

    has_thinking = False
    eos_token_ids = frozenset({"<eos>"})

    def apply_chat_template(self, messages, add_generation_prompt, enable_thinking):
        words = [w for m in messages for w in [f"<{m['role']}>", *m["content"].split()]]
        return [*words, "<reply>"] if add_generation_prompt else words

    def decode(self, tokens):
        return " ".join(tokens)


@pytest.fixture
def lookup(mlx, monkeypatch):
    """The model answers `reply` to the cleanup of `ну раз два три четыре пять шесть семь`."""
    monkeypatch.setattr(sys.modules["mlx_lm.sample_utils"], "make_sampler", lambda temp: lambda logits: logits)

    def load(name):
        model, _ = FakeMLX.load(mlx, name)
        return model, WordTokenizer()

    mlx.load = load
    sys.modules["mlx_lm"].load = load

    def answer(reply):
        llm = LLM("gemma", cache_prefix=True)
        llm.chat("чисти", "ну", EXAMPLES)  # loads the model and caches the prefix
        model, tokenizer = llm_module._loaded["gemma"]
        prompt = tokenizer.apply_chat_template(
            [
                {"role": "system", "content": "чисти"},
                {"role": "user", "content": "ну э привет"},
                {"role": "assistant", "content": "Привет."},
                {"role": "user", "content": RAW},
            ],
            add_generation_prompt=True,
            enable_thinking=False,
        )
        model.script = [*prompt, *reply.split(), "<eos>"]
        model.passes = 0
        return llm.chat("чисти", RAW, EXAMPLES, max_growth=1.5, lookup=True), model

    return answer


RAW = "ну раз два три четыре пять шесть семь"


def test_lookup_copies_the_input_several_tokens_a_pass(lookup):
    text, model = lookup("раз два три четыре пять шесть семь")
    assert text == "раз два три четыре пять шесть семь"
    assert model.passes - 1 < 8 / 2  # the first pass is the prefill of the dictation's own message


def test_lookup_throws_out_a_wrong_draft_and_keeps_the_model_reply(lookup):
    text, _ = lookup("раз два, три пять шесть семь восемь")
    assert text == "раз два, три пять шесть семь восемь"


def test_lookup_leaves_the_saved_prefix_clean(lookup):
    lookup("раз два три четыре")
    [prefix] = llm_module._prefixes["gemma"].values()
    assert prefix[0].tokens == ["<system>", "чисти", "<user>", "ну", "э", "привет", "<assistant>", "Привет."]


def test_lookup_stops_a_runaway_at_the_limit(lookup):
    with pytest.raises(ValueError):
        lookup(" ".join(["бла"] * 100))
