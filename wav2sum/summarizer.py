from __future__ import annotations

import logging

import ollama

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemma4:26b-a4b-it-q4_K_M"

SYSTEM_PROMPT = """\
Ты — ассистент для обработки протоколов рабочих совещаний.
Тебе дан транскрипт аудиозаписи созвона с разметкой по спикерам.
Составь по нему структурированное саммари на русском языке.

Саммари должно содержать:
1. **Тема встречи** — одно предложение.
2. **Участники** — перечисли спикеров.
3. **Ключевые обсуждения** — основные темы и позиции участников (тезисно, 3-7 пунктов).
4. **Принятые решения** — что конкретно решили.
5. **Задачи и дедлайны** — кто, что, до когда (если обсуждалось).
6. **Открытые вопросы** — что осталось нерешённым.

Отвечай только на русском. Будь лаконичен и точен.\
"""


class Summarizer:
    """Meeting transcript summarization via a local Ollama model."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        logger.info("Summarizer model: %s", self.model)

    def summarize(self, transcript: str) -> str:
        """Send the transcript to Ollama and return the summary."""
        user_msg = f"Вот транскрипт созвона:\n\n{transcript}"

        logger.info("Sending %d chars to Ollama (%s) …", len(transcript), self.model)

        response = ollama.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )

        summary: str = response["message"]["content"]
        logger.info("Summary received (%d chars).", len(summary))
        return summary
