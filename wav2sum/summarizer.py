import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wav2sum.language import EN, RU
from wav2sum.llm import LLM

logger = logging.getLogger(__name__)

RESERVED_TOKENS = 4096
MAX_CHAPTERS = 8
SNAP_SECONDS = 5

ASR_NOTE_RU = """\
Транскрипт получен автоматическим распознаванием речи и содержит ошибки: слова, похожие по звучанию, \
искажённые термины и имена, оборванные фразы. Понимай текст по смыслу и не переноси бессмыслицу дальше. \
Искажённый термин исправляй, только если однозначно понятно, какое слово имелось в виду, и не заменяй его \
другим знакомым словом. Если фрагмент непонятен, пропусти его.\
"""

SUMMARY_PROMPT_RU = f"""\
Ты составляешь саммари созвона на русском языке: рабочего созвона, 1:1, интервью, урока или другого разговора.
Тебе дан транскрипт с разметкой по спикерам (или заметки по частям созвона).
{ASR_NOTE_RU}

Формат — эти разделы, в этом порядке:
**Тема** — одно предложение.
**Обсуждения** — главные темы и позиции участников, тезисно, не больше 7 пунктов.
Раздел под суть встречи, со своим названием, — то, что из этой встречи стоит вынести и что не ложится в другие \
разделы: например, советы и замечания преподавателя на уроке, впечатления и следующий шаг на интервью, обратная \
связь на 1:1.
**Решения** — что конкретно решили.
**Задачи** — только то, о чём договорились: исполнитель (имя из разметки), что сделать, срок, если назван.
**Открытые вопросы** — что осталось нерешённым.

Правила:
- Пиши только то, что прямо сказано в разговоре. Не додумывай причины, последствия и детали.
- Числа, даты, сроки, имена и названия бери только из транскрипта. Если срок не назван, не указывай его.
- Если для раздела, кроме «Тема» и «Обсуждения», в разговоре ничего нет, не пиши этот раздел вовсе, даже \
заголовок. Не пиши раздел, чтобы сказать, что в нём ничего нет («Нет», «Решений не принято»), и не заполняй его \
ради заполнения.
- Кто говорит, видно из разметки. Отдельно участников не перечисляй.
- Лучше коротко и точно, чем подробно и с домыслами.\
"""

CHUNK_PROMPT_RU = f"""\
Тебе дан фрагмент транскрипта созвона с разметкой по спикерам.
{ASR_NOTE_RU}

Это только ЧАСТЬ встречи. Не пиши итоговое саммари.
Выпиши из фрагмента, тезисно и на русском:
- обсуждаемые темы и позиции участников;
- принятые решения;
- задачи, договорённости и дедлайны (кто и что);
- советы, замечания и обратная связь (кто кому);
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
You write a summary of a call in English: a work call, a 1:1, an interview, a lesson or another conversation.
You are given a speaker-labelled transcript (or notes on parts of the call).
{ASR_NOTE_EN}

Format: these sections, in this order:
**Topic** — one sentence.
**Discussion** — the main topics and each side's position, as bullet points, at most 7.
A section for what the meeting was about, under a title of its own: what is worth taking away from it that doesn't \
fit the other sections, such as a teacher's advice and remarks in a lesson, impressions and the next step in an \
interview, feedback in a 1:1.
**Decisions** — what was actually decided.
**Action items** — only what was agreed: owner (name from the labels), what to do, deadline if one was named.
**Open questions** — what was left unresolved.

Rules:
- Write only what was actually said. Don't make up reasons, consequences or details.
- Take numbers, dates, deadlines, names and titles only from the transcript. If no deadline was named, give none.
- If the call had nothing for a section other than Topic and Discussion, leave that section out, heading and \
all. Don't write a section to say it is empty ("None", "No decisions were made"), and don't fill one for the sake \
of it.
- The labels show who speaks. Don't list participants separately.
- Short and accurate beats detailed and speculative.\
"""

CHUNK_PROMPT_EN = f"""\
You are given a part of a speaker-labelled transcript of a call.
{ASR_NOTE_EN}

This is only PART of the meeting. Don't write a final summary.
List from this part, as bullet points in English:
- topics discussed and each side's position;
- decisions made;
- action items, agreements and deadlines (who does what);
- advice, remarks and feedback (who to whom);
- open questions.
Skip points this part doesn't have. Keep speaker names. Take numbers, dates and names only from this part; \
don't make anything up.\
"""


CHAPTERS_PROMPT_RU = f"""\
Тебе дан транскрипт созвона с таймкодами реплик.
Раздели разговор на главы по темам, по порядку, не больше {MAX_CHAPTERS}. Первая глава начинается с первой реплики, \
новая — там, где разговор переходит к другой теме.
Ответ — только строки глав, каждая вида «- мм:сс — тема в нескольких словах», где мм:сс — таймкод начала первой \
реплики главы, скопированный из транскрипта. Без вступления и пояснений.\
"""

CHAPTERS_PROMPT_EN = f"""\
You are given a call transcript with timestamps on each utterance.
Split the conversation into chapters by topic, in order, at most {MAX_CHAPTERS}. The first chapter starts at the \
first utterance, and a new one where the conversation moves on to another topic.
Reply with chapter lines only, each as "- mm:ss — the topic in a few words", where mm:ss is the start timestamp of \
the chapter's first utterance, copied from the transcript. No intro and no comments.\
"""

MERGE_CHAPTERS_PROMPT_RU = f"""\
Тебе дан список глав созвона, по строке «- мм:сс — тема». Глав слишком много. Объедини соседние главы с близкими \
темами так, чтобы осталось не больше {MAX_CHAPTERS}. У объединённой главы время её первой строки, тема — общая для \
объединённых. Ответ — только строки глав в том же формате, без вступления и пояснений.\
"""

MERGE_CHAPTERS_PROMPT_EN = f"""\
You are given a list of a call's chapters, one "- mm:ss — topic" line each. There are too many. Merge neighbouring \
chapters with related topics so that at most {MAX_CHAPTERS} remain. A merged chapter takes the time of its first line \
and a topic that covers the merged ones. Reply with chapter lines only, in the same format, no intro and no comments.\
"""


@dataclass(frozen=True)
class Prompts:
    summary: str
    chunk: str
    glossary: str
    transcript: str
    notes: str
    part: str
    chapters: str
    merge_chapters: str
    chapters_title: str


PROMPTS = {
    RU: Prompts(
        SUMMARY_PROMPT_RU,
        CHUNK_PROMPT_RU,
        "Термины и имена, которые могут встречаться в разговоре, пиши именно так: {}.",
        "Транскрипт созвона",
        "Заметки по частям созвона",
        "Часть",
        CHAPTERS_PROMPT_RU,
        MERGE_CHAPTERS_PROMPT_RU,
        "Главы",
    ),
    EN: Prompts(
        SUMMARY_PROMPT_EN,
        CHUNK_PROMPT_EN,
        "Terms and names that may come up in the call; spell them exactly like this: {}.",
        "Call transcript",
        "Notes on parts of the call",
        "Part",
        CHAPTERS_PROMPT_EN,
        MERGE_CHAPTERS_PROMPT_EN,
        "Chapters",
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


def chapters(transcript: str, llm: LLM, language: str = RU) -> str:
    """Chapters from a timestamped transcript, in a call of their own without thinking: with the timestamps and
    a chapters section in the summary prompt, the summary's thinking looped on re-checking them."""
    prompts = PROMPTS[language]
    starts = {_seconds(t): t for t in re.findall(r"^\[(\S+) – ", transcript, re.MULTILINE)}
    if not starts:
        return ""

    def at_starts(reply: str) -> list[str]:
        # The model sometimes writes a time a second or two off; it is moved to the nearest utterance start.
        lines = []
        for line in reply.splitlines():
            time, _, topic = line.removeprefix("- ").partition(" ")
            if line.startswith("- ") and re.fullmatch(r"(\d+:)?\d+:\d\d", time):
                nearest = min(starts, key=lambda start: abs(start - _seconds(time)))
                if abs(nearest - _seconds(time)) <= SNAP_SECONDS:
                    lines.append(f"- {starts[nearest]} {topic}")
        return lines

    lines = []
    try:
        for chunk in chunk_transcript(transcript, (llm.num_ctx - RESERVED_TOKENS) * 2 // 3, llm.count_tokens):
            lines += at_starts(llm.chat(prompts.chapters, chunk, max_growth=0.2, think=False))
        if len(lines) > MAX_CHAPTERS:
            # The model doesn't keep to the limit, and a long call gets up to that many from each part.
            lines = at_starts(llm.chat(prompts.merge_chapters, "\n".join(lines), max_growth=1, think=False)) or lines
    except ValueError as e:
        logger.warning("No chapters: %s", e)
        return ""
    return f"**{prompts.chapters_title}**\n" + "\n".join(lines) if lines else ""


def _seconds(clock: str) -> int:
    return sum(int(part) * 60**i for i, part in enumerate(reversed(clock.split(":"))))


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
