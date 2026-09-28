import logging

from wav2sum.llm import LLM

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.5
RESERVED_TOKENS = 4096

SUMMARY_PROMPT = """\
Ты — ассистент для обработки протоколов рабочих совещаний.
Тебе дан транскрипт созвона с разметкой по спикерам (или заметки по его частям).
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

CHUNK_PROMPT = """\
Тебе дан фрагмент транскрипта рабочего созвона с разметкой по спикерам.
Это только ЧАСТЬ встречи. Не пиши итоговое саммари.
Выпиши из фрагмента, тезисно и на русском:
- обсуждаемые темы и позиции участников;
- любые принятые решения;
- любые задачи, договорённости и дедлайны (с указанием, кто и что);
- открытые/нерешённые вопросы.
Сохраняй имена спикеров. Не выдумывай того, чего нет во фрагменте.\
"""


def summarize(transcript: str, llm: LLM) -> str:
    max_chars = int((llm.num_ctx - RESERVED_TOKENS) * CHARS_PER_TOKEN)
    if len(transcript) <= max_chars:
        return llm.chat(SUMMARY_PROMPT, f"Транскрипт созвона:\n\n{transcript}")

    chunks = chunk_transcript(transcript, max_chars * 2 // 3)
    logger.info("Long transcript (%d chars): summarizing %d parts first.", len(transcript), len(chunks))
    notes = [llm.chat(CHUNK_PROMPT, chunk) for chunk in chunks]
    joined = "\n\n".join(f"--- Часть {i} ---\n{n}" for i, n in enumerate(notes, 1))
    return llm.chat(SUMMARY_PROMPT, f"Заметки по частям созвона:\n\n{joined}")


def chunk_transcript(transcript: str, max_chars: int) -> list[str]:
    chunks: list[list[str]] = [[]]
    size = 0
    for utterance in transcript.split("\n\n"):
        if chunks[-1] and size + len(utterance) > max_chars:
            chunks.append([])
            size = 0
        chunks[-1].append(utterance)
        size += len(utterance) + 2
    return ["\n\n".join(c) for c in chunks]
