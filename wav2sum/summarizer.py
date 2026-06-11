from __future__ import annotations

import logging

import ollama

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemma4:26b-a4b-it-q4_K_M"

# Above this many characters we summarize in a map-reduce fashion instead of
# one shot, so multi-hour calls don't silently overflow the model context.
MAX_SINGLE_PASS_CHARS = 24_000
CHUNK_CHARS = 16_000

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

# Used on each chunk of a long transcript before the final reduce step.
MAP_PROMPT = """\
Тебе дан фрагмент транскрипта рабочего созвона с разметкой по спикерам.
Это только ЧАСТЬ встречи. Не пиши итоговое саммари.
Выпиши из фрагмента, тезисно и на русском:
- обсуждаемые темы и позиции участников;
- любые принятые решения;
- любые задачи, договорённости и дедлайны (с указанием, кто и что);
- открытые/нерешённые вопросы.
Сохраняй имена спикеров. Не выдумывай того, чего нет во фрагменте.\
"""


class Summarizer:
    """Meeting transcript summarization via a local Ollama model."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        logger.info("Summarizer model: %s", self.model)

    def summarize(self, transcript: str) -> str:
        """Summarize the transcript, map-reducing it if it is long."""
        if len(transcript) <= MAX_SINGLE_PASS_CHARS:
            logger.info("Single-pass summary (%d chars).", len(transcript))
            return self._summarize_text(transcript)

        chunks = _chunk_transcript(transcript, CHUNK_CHARS)
        logger.info(
            "Long transcript (%d chars) → map-reduce over %d chunks.",
            len(transcript), len(chunks),
        )

        notes: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            logger.info("Summarizing chunk %d/%d …", i, len(chunks))
            notes.append(self._chat(MAP_PROMPT, chunk))

        combined = "\n\n".join(
            f"--- Заметки по части {i} ---\n{n}" for i, n in enumerate(notes, 1)
        )
        logger.info("Reducing %d chunk notes into final summary.", len(notes))
        return self._summarize_text(combined)

    # ── internals ────────────────────────────────────────────

    def _summarize_text(self, text: str) -> str:
        return self._chat(SYSTEM_PROMPT, f"Вот транскрипт созвона:\n\n{text}")

    def _chat(self, system: str, user: str) -> str:
        logger.info("Sending %d chars to Ollama (%s) …", len(user), self.model)
        response = ollama.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content: str = response["message"]["content"]
        logger.info("Received %d chars.", len(content))
        return content


def _chunk_transcript(transcript: str, chunk_chars: int) -> list[str]:
    """Pack whole utterances (split on blank lines) into bounded chunks."""
    utterances = transcript.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for utt in utterances:
        # +2 accounts for the "\n\n" separator re-added on join.
        if current and size + len(utt) + 2 > chunk_chars:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(utt)
        size += len(utt) + 2

    if current:
        chunks.append("\n\n".join(current))
    return chunks
