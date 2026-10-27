import copy
import gc
import logging
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace

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
# Tokens a lookup drafts from the prompt per pass: 4 was the fastest on real cleanups (CHU-120).
LOOKUP_TOKENS = 4


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
        self,
        system: str,
        user: str,
        examples: list[tuple[str, str]] = (),
        max_growth: float | None = None,
        lookup: bool = False,
    ) -> str:
        """max_growth caps the reply at that many times the input's tokens, so a looping model stops early.
        lookup drafts the reply from the prompt and checks several tokens a pass: faster for a reply that copies it."""
        logger.debug("→ %s: %d chars", self.model, len(user))
        shots = [
            {"role": role, "content": text}
            for pair in examples
            for role, text in zip(("user", "assistant"), pair, strict=True)
        ]
        messages = [{"role": "system", "content": system}, *shots, {"role": "user", "content": user}]
        if self.timeout is None:
            future = _worker.submit(self._generate, messages, max_growth, lookup)
        else:
            future = Future()
            _urgent.put((future, partial(self._generate, messages, max_growth, lookup)))
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

    def _generate(self, messages: list[dict], max_growth: float | None, lookup: bool) -> str:
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        if timer := _timers.pop(self.model, None):
            timer.cancel()
        model, tokenizer = _load(self.model)
        prompt = whole = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, enable_thinking=bool(self.think)
        )
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
        sampler = make_sampler(self.temperature)
        for response in (
            _lookup_generate(model, tokenizer, whole, prompt, cache=cache, limit=limit, sampler=sampler)
            if lookup
            else stream_generate(
                model,
                tokenizer,
                prompt,
                max_tokens=limit,
                sampler=sampler,
                prompt_cache=cache,
                prompt_progress_callback=yield_to_urgent,
            )
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


def _lookup_generate(model, tokenizer, whole: list, prompt: list, *, cache: list | None, limit: int, sampler):
    """Speculative decoding with drafts copied from the prompt: the tokens that followed the last place where
    the reply's latest tokens occur. The model checks a draft in one pass and keeps the tokens it would have
    sampled itself, so the reply is the model's own; a cleanup mostly repeats its input, so most drafts pass."""
    import mlx.core as mx
    from mlx_lm.models.cache import trim_prompt_cache

    cache = cache or _trimmable_cache(model)
    if len(prompt) > 1:
        model(mx.array([prompt[:-1]]), cache=cache)
    tokens, reply = list(whole), []
    while len(reply) < limit:
        draft = _draft(tokens, min(LOOKUP_TOKENS, limit - len(reply) - 1))
        logits = model(mx.array([[tokens[-1], *draft]]), cache=cache)[0]
        sampled = sampler(logits - mx.logsumexp(logits, axis=-1, keepdims=True)).tolist()
        n = 0
        while n < len(draft) and sampled[n] == draft[n]:
            n += 1
        trim_prompt_cache(cache, len(draft) - n)
        for token in sampled[: n + 1]:
            if token in tokenizer.eos_token_ids:
                yield SimpleNamespace(text=tokenizer.decode(reply))
                return
            tokens.append(token)
            reply.append(token)
    yield SimpleNamespace(text=tokenizer.decode(reply))


def _draft(tokens: list, size: int) -> list:
    for n in (3, 2, 1):
        tail = tokens[-n:]
        for i in range(len(tokens) - n - 1, -1, -1):
            if tokens[i : i + n] == tail:
                return tokens[i + n : i + n + size]
    return []


def _trimmable_cache(model) -> list:
    """Gemma's sliding-window layers keep a rotating cache that can't take back tokens once the prompt is longer
    than the window; a plain cache can, and the model's mask keeps the same window. At temperature 0 the reply
    still differs by a few words from the rotating cache's in 7 of 50 cleanups (CHU-120)."""
    from mlx_lm.models.cache import KVCache, make_prompt_cache

    return [KVCache() for _ in make_prompt_cache(model)]


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

        # Trimmable, so a cleanup's lookup can take back the draft tokens the model rejects.
        cache = _trimmable_cache(model)
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
