import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wav2sum.language import EN, RU
from wav2sum.llm import LLM

logger = logging.getLogger(__name__)

RESERVED_TOKENS = 4096

ASR_NOTE_RU = """\
Транскрипт получен автоматическим распознаванием речи и содержит ошибки: слова, похожие по звучанию, \
искажённые термины и имена, оборванные фразы. Понимай текст по смыслу и не переноси бессмыслицу дальше. \
Искажённый термин исправляй, только если однозначно понятно, какое слово имелось в виду, и не заменяй его \
другим знакомым словом. Если фрагмент непонятен, пропусти его.\
"""

SUMMARY_PROMPT_RU = f"""\
Ты составляешь саммари рабочего созвона на русском языке.
Тебе дан транскрипт с разметкой по спикерам (или заметки по частям созвона).
{ASR_NOTE_RU}

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

CHUNK_PROMPT_RU = f"""\
Тебе дан фрагмент транскрипта рабочего созвона с разметкой по спикерам.
{ASR_NOTE_RU}

Это только ЧАСТЬ встречи. Не пиши итоговое саммари.
Выпиши из фрагмента, тезисно и на русском:
- обсуждаемые темы и позиции участников;
- принятые решения;
- задачи, договорённости и дедлайны (кто и что);
- открытые вопросы.
Пропускай пункты, которых во фрагменте нет. Сохраняй имена спикеров. Числа, даты и имена бери только из \
фрагмента, ничего не додумывай.\
"""

ASR_NOTE_EN = """\
The transcript comes from automatic speech recognition and has errors: sound-alike words, garbled terms and \
names, cut-off phrases. Read it for meaning and don't carry nonsense over. Fix a garbled term only when it is \
unambiguous which word was meant, and don't swap it for some other familiar word. Skip passages you can't make \
sense of.\
"""

SUMMARY_PROMPT_EN = f"""\
You write a summary of a work call in English.
You are given a speaker-labelled transcript (or notes on parts of the call).
{ASR_NOTE_EN}

Format: exactly these sections, in this order:
**Topic** — one sentence.
**Discussion** — the main topics and each side's position, as bullet points, at most 7.
**Decisions** — what was actually decided.
**Action items** — only what was agreed: owner (name from the labels), what to do, deadline if one was named.
**Open questions** — what was left unresolved.

Rules:
- Write only what was actually said. Don't make up reasons, consequences or details.
- Take numbers, dates, deadlines, names and titles only from the transcript. If no deadline was named, give none.
- If the call had nothing for a section, write "Not discussed" in it. That is a fine answer; don't fill a \
section for the sake of it.
- The labels show who speaks. Don't list participants separately.
- Short and accurate beats detailed and speculative.\
"""

CHUNK_PROMPT_EN = f"""\
You are given a part of a speaker-labelled transcript of a work call.
{ASR_NOTE_EN}

This is only PART of the meeting. Don't write a final summary.
List from this part, as bullet points in English:
- topics discussed and each side's position;
- decisions made;
- action items, agreements and deadlines (who does what);
- open questions.
Skip points this part doesn't have. Keep speaker names. Take numbers, dates and names only from this part; \
don't make anything up.\
"""


@dataclass(frozen=True)
class Prompts:
    summary: str
    chunk: str
    glossary: str
    transcript: str
    notes: str
    part: str


PROMPTS = {
    RU: Prompts(
        SUMMARY_PROMPT_RU,
        CHUNK_PROMPT_RU,
        "Термины и имена, которые могут встречаться в разговоре, пиши именно так: {}.",
        "Транскрипт созвона",
        "Заметки по частям созвона",
        "Часть",
    ),
    EN: Prompts(
        SUMMARY_PROMPT_EN,
        CHUNK_PROMPT_EN,
        "Terms and names that may come up in the call; spell them exactly like this: {}.",
        "Call transcript",
        "Notes on parts of the call",
        "Part",
    ),
}


def summarize(transcript: str, llm: LLM, terms: Sequence[str] = (), language: str = RU) -> str:
    prompts = PROMPTS[language]
    glossary = "\n\n" + prompts.glossary.format(", ".join(terms)) if terms else ""
    max_tokens = llm.num_ctx - RESERVED_TOKENS
    user = f"{prompts.transcript}:\n\n{transcript}"
    if llm.count_tokens(prompts.summary + glossary + user) <= max_tokens:
        return llm.chat(prompts.summary + glossary, user)

    chunks = chunk_transcript(transcript, max_tokens * 2 // 3, llm.count_tokens)
    logger.info("Long transcript (%d chars): summarizing %d parts first.", len(transcript), len(chunks))
    notes = [llm.chat(prompts.chunk + glossary, chunk) for chunk in chunks]
    joined = "\n\n".join(f"--- {prompts.part} {i} ---\n{n}" for i, n in enumerate(notes, 1))
    return llm.chat(prompts.summary + glossary, f"{prompts.notes}:\n\n{joined}")


def chunk_transcript(transcript: str, max_tokens: int, count_tokens: Callable[[str], int]) -> list[str]:
    chunks: list[list[str]] = [[]]
    size = 0
    for utterance in transcript.split("\n\n"):
        tokens = count_tokens(utterance)
        if chunks[-1] and size + tokens > max_tokens:
            chunks.append([])
            size = 0
        chunks[-1].append(utterance)
        size += tokens
    return ["\n\n".join(c) for c in chunks]
