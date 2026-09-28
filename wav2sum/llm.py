import logging
from dataclasses import dataclass

import ollama

logger = logging.getLogger(__name__)


@dataclass
class LLM:
    model: str
    num_ctx: int = 8192
    temperature: float = 0.3
    think: bool | None = None
    keep_alive: float | str | None = None

    def chat(self, system: str, user: str, examples: list[tuple[str, str]] = ()) -> str:
        logger.debug("→ %s: %d chars", self.model, len(user))
        shots = [
            {"role": role, "content": text}
            for pair in examples
            for role, text in zip(("user", "assistant"), pair, strict=True)
        ]
        response = ollama.chat(
            model=self.model,
            messages=[{"role": "system", "content": system}, *shots, {"role": "user", "content": user}],
            options={"num_ctx": self.num_ctx, "temperature": self.temperature},
            think=self.think,
            keep_alive=self.keep_alive,
        )
        return response.message.content.strip()

    def unload(self) -> None:
        ollama.generate(model=self.model, keep_alive=0)
