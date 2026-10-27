import gc
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# One thread runs every generation: MLX streams are per thread, and the GPU runs one model at a time anyway.
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm")
_loaded: dict[str, tuple] = {}
_timers: dict[str, threading.Timer] = {}


@dataclass
class LLM:
    model: str
    num_ctx: int = 8192
    temperature: float = 0.3
    think: bool | None = None
    keep_alive: float | None = None  # idle seconds before the weights are freed; None keeps them until unload()
    timeout: float | None = None

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
        future = _worker.submit(self._generate, messages, max_growth)
        try:
            return future.result(self.timeout)
        except TimeoutError:
            # A dictation that gave up waiting behind a summary must not run later; one already running
            # (say, a slow first download) finishes with nobody waiting, so its failure goes to the log.
            if not future.cancel():
                future.add_done_callback(_log_failure)
            raise

    def unload(self) -> None:
        _worker.submit(_unload, self.model).result()

    def _generate(self, messages: list[dict], max_growth: float | None) -> str:
        from mlx_lm import generate
        from mlx_lm.sample_utils import make_sampler

        if timer := _timers.pop(self.model, None):
            timer.cancel()
        model, tokenizer = _load(self.model)
        prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, enable_thinking=bool(self.think))
        limit = self.num_ctx
        if max_growth is not None:
            limit = min(limit, int(max_growth * len(tokenizer.encode(messages[-1]["content"]))) + 64)
        text = generate(model, tokenizer, prompt, max_tokens=limit, sampler=make_sampler(self.temperature))
        if self.keep_alive is not None:
            _timers[self.model] = timer = threading.Timer(self.keep_alive, lambda: _worker.submit(_unload, self.model))
            timer.daemon = True
            timer.start()
        if max_growth is not None and len(tokenizer.encode(text)) >= limit:
            raise ValueError(f"LLM reply hit the {limit}-token limit")
        if tokenizer.has_thinking:
            text = text.rpartition(tokenizer.think_end)[2]
        return text.strip()


def _log_failure(future) -> None:
    if error := future.exception():
        logger.error("LLM failed after the caller stopped waiting", exc_info=error)


def memory_mb() -> int:
    """Memory held by the loaded LLM weights."""
    from mlx.utils import tree_flatten

    return sum(v.nbytes for model, _ in list(_loaded.values()) for _, v in tree_flatten(model.parameters())) // 2**20


def _load(name: str) -> tuple:
    if name not in _loaded:
        import mlx.core as mx
        from mlx_lm import load

        logger.info("Loading %s …", name)
        model, tokenizer = load(name)
        mx.eval(model.parameters())
        _loaded[name] = model, tokenizer
    return _loaded[name]


def _unload(name: str) -> None:
    if timer := _timers.pop(name, None):
        timer.cancel()
    if _loaded.pop(name, None):
        import mlx.core as mx

        gc.collect()
        mx.clear_cache()
        logger.info("Unloaded %s", name)
