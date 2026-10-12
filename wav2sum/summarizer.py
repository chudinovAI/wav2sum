import logging
from collections.abc import Sequence

from wav2sum.llm import LLM

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.5
RESERVED_TOKENS = 4096

ASR_NOTE = """\
Транскрипт получен автоматическим распознаванием речи и содержит ошибки: слова, похожие по звучанию, \
искажённые термины и имена, оборванные фразы. Понимай текст по смыслу и не переноси бессмыслицу дальше. \
Искажённый термин исправляй, только если однозначно понятно, какое слово имелось в виду, и не заменяй его \
другим знакомым словом. Если фрагмент непонятен, пропусти его.\
"""

SUMMARY_PROMPT = f"""\
Ты составляешь саммари рабочего созвона на русском языке.
Тебе дан транскрипт с разметкой по спикерам (или заметки по частям созвона).
{ASR_NOTE}

Формат — ровно эти разделы, в этом порядке:
**Тема** — одно предложение.
**Обсуждения** — главные темы и позиции участников, тезисно, не больше 7 пунктов.
**Решения** — что конкретно решили.
**Задачи** — только то, о чём договорились: исполнитель (имя из разметки), что сделать, срок, если назван.
**Открытые вопросы** — что осталось нерешённым.

Правила:
- Пиши только то, что прямо сказано в разговоре. Не додумывай причины, последствия и детали.
- Числа, даты, сроки, имена и названия бери только из транскрипта. Если срок не назван, не указывай его.
- Если для раздела в разговоре ничего нет, напиши в нём «Не обсуждалось». Это нормальный ответ, не заполняй \
раздел ради заполнения.
- Кто говорит, видно из разметки. Отдельно участников не перечисляй.
- Лучше коротко и точно, чем подробно и с домыслами.\
"""

CHUNK_PROMPT = f"""\
Тебе дан фрагмент транскрипта рабочего созвона с разметкой по спикерам.
{ASR_NOTE}

Это только ЧАСТЬ встречи. Не пиши итоговое саммари.
Выпиши из фрагмента, тезисно и на русском:
- обсуждаемые темы и позиции участников;
- принятые решения;
- задачи, договорённости и дедлайны (кто и что);
- открытые вопросы.
Пропускай пункты, которых во фрагменте нет. Сохраняй имена спикеров. Числа, даты и имена бери только из \
фрагмента, ничего не додумывай.\
"""


def summarize(transcript: str, llm: LLM, terms: Sequence[str] = ()) -> str:
    glossary = f"\n\nТермины и имена, которые могут встречаться в разговоре, пиши именно так: {', '.join(terms)}."
    summary_prompt, chunk_prompt = (
        (SUMMARY_PROMPT + glossary, CHUNK_PROMPT + glossary) if terms else (SUMMARY_PROMPT, CHUNK_PROMPT)
    )
    max_chars = int((llm.num_ctx - RESERVED_TOKENS) * CHARS_PER_TOKEN)
    if len(transcript) <= max_chars:
        return llm.chat(summary_prompt, f"Транскрипт созвона:\n\n{transcript}")

    chunks = chunk_transcript(transcript, max_chars * 2 // 3)
    logger.info("Long transcript (%d chars): summarizing %d parts first.", len(transcript), len(chunks))
    notes = [llm.chat(chunk_prompt, chunk) for chunk in chunks]
    joined = "\n\n".join(f"--- Часть {i} ---\n{n}" for i, n in enumerate(notes, 1))
    return llm.chat(summary_prompt, f"Заметки по частям созвона:\n\n{joined}")


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
