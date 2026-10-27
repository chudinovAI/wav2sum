import copy
import gc
import logging
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

logger = logging.getLogger(__name__)

# One thread runs every generation: MLX streams are per thread, and the GPU runs one model at a time anyway.
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm")
# Dictations (chats with a timeout) wait here too, so a summary runs them between its tokens.
_urgent: queue.SimpleQueue = queue.SimpleQueue()
_loaded: dict[str, tuple] = {}
_timers: dict[str, threading.Timer] = {}
# Dictation reruns the same system prompt and examples each time; their KV cache saves that prefill.
# One prefix per app style in use (chat, code, email, a command); least recently used goes first.
MAX_PREFIXES = 4
_prefixes: dict[str, dict[tuple, list]] = {}


@dataclass
class LLM:
    model: str
    num_ctx: int = 8192
    temperature: float = 0.3
    think: bool | None = None
    keep_alive: float | None = None  # idle seconds before the weights are freed; None keeps them until unload()
    timeout: float | None = None
    cache_prefix: bool = False  # keep the KV cache of all but the last message while the weights are loaded

    def chat(
        self, system: str, user: str, examples: list[tuple[str, str]] = (), max_growth: float | None = None
    ) -> str:
        """max_growth caps the reply at that many times the input's tokens, so a looping model stops early."""
        logger.debug("→ %s: %d chars", self.model, len(user))
        shots = [
            {"role": role, "content": text}
            for pair in examples
            for role, text in zip(("user", "assistant"), pair, strict=True)
        ]
        messages = [{"role": "system", "content": system}, *shots, {"role": "user", "content": user}]
        if self.timeout is None:
            future = _worker.submit(self._generate, messages, max_growth)
        else:
            future = Future()
            _urgent.put((future, partial(self._generate, messages, max_growth)))
            _worker.submit(_run_urgent)
        try:
            return future.result(self.timeout)
        except TimeoutError:
            # A dictation that gave up waiting behind a summary must not run later; one already running
            # (say, a slow first download) finishes with nobody waiting, so its failure goes to the log.
            if not future.cancel():
                future.add_done_callback(_log_failure)
            raise

    def count_tokens(self, text: str) -> int:
        return _worker.submit(lambda: len(_load(self.model)[1].encode(text))).result()

    def unload(self) -> None:
        _worker.submit(_unload, self.model).result()

    def _generate(self, messages: list[dict], max_growth: float | None) -> str:
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        if timer := _timers.pop(self.model, None):
            timer.cancel()
        model, tokenizer = _load(self.model)
        prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=bool(self.think))
        limit = self.num_ctx
        if max_growth is not None:
            limit = min(limit, int(max_growth * len(tokenizer.encode(messages[-1]["content"]))) + 64)
        cache = None
        if self.cache_prefix:
            prefix = tokenizer.apply_chat_template(
                messages[:-1], add_generation_prompt=False, enable_thinking=bool(self.think)
            )
            if prompt[: len(prefix)] == prefix:
                # Generation writes into the cache, so it gets a copy and the saved prefix stays clean.
                cache = copy.deepcopy(_prefix_cache(self.model, model, tuple(prefix)))
                prompt = prompt[len(prefix) :]
            else:
                logger.debug("The chat template renders the prefix differently; running the whole prompt")
        # A summary lets waiting dictations run between its prefill chunks and tokens; dictations wait their turn.
        yield_to_urgent = (lambda *_: _run_urgent()) if self.timeout is None else None
        text = ""
        for response in stream_generate(
            model,
            tokenizer,
            prompt,
            max_tokens=limit,
            sampler=make_sampler(self.temperature),
            prompt_cache=cache,
            prompt_progress_callback=yield_to_urgent,
        ):
            text += response.text
            if yield_to_urgent:
                yield_to_urgent()
        if self.keep_alive is not None:
            # A dictation on the same model, run in the middle of this summary, has set its own timer.
            if timer := _timers.pop(self.model, None):
                timer.cancel()
            _timers[self.model] = timer = threading.Timer(self.keep_alive, lambda: _worker.submit(_unload, self.model))
            timer.daemon = True
            timer.start()
        if max_growth is not None and len(tokenizer.encode(text)) >= limit:
            raise ValueError(f"LLM reply hit the {limit}-token limit")
        if tokenizer.has_thinking:
            text = text.rpartition(tokenizer.think_end)[2]
        return text.strip()


def _run_urgent() -> None:
    while not _urgent.empty():
        future, generate = _urgent.get()
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(generate())
            except Exception as e:
                future.set_exception(e)


def _log_failure(future) -> None:
    if error := future.exception():
        logger.error("LLM failed after the caller stopped waiting", exc_info=error)


def memory_mb() -> int:
    """Memory held by the loaded LLM weights and their prompt prefix caches."""
    from mlx.utils import tree_flatten

    weights = sum(v.nbytes for model, _ in list(_loaded.values()) for _, v in tree_flatten(model.parameters()))
    caches = sum(layer.nbytes for c in list(_prefixes.values()) for cache in list(c.values()) for layer in cache)
    return (weights + caches) // 2**20


def _load(name: str) -> tuple:
    if name not in _loaded:
        import mlx.core as mx
        from mlx_lm import load

        logger.info("Loading %s …", name)
        model, tokenizer = load(name)
        mx.eval(model.parameters())
        _loaded[name] = model, tokenizer
    return _loaded[name]


def _prefix_cache(name: str, model, prefix: tuple) -> list:
    caches = _prefixes.setdefault(name, {})
    if prefix in caches:
        caches[prefix] = caches.pop(prefix)  # the most recently used goes last
    else:
        import mlx.core as mx
        from mlx_lm.models.cache import make_prompt_cache

        cache = make_prompt_cache(model)
        model(mx.array([prefix]), cache=cache)
        mx.eval([layer.state for layer in cache])
        caches[prefix] = cache
        if len(caches) > MAX_PREFIXES:
            del caches[next(iter(caches))]
    return caches[prefix]


def _unload(name: str) -> None:
    if timer := _timers.pop(name, None):
        timer.cancel()
    _prefixes.pop(name, None)
    if _loaded.pop(name, None):
        import mlx.core as mx

        gc.collect()
        mx.clear_cache()
        logger.info("Unloaded %s", name)
