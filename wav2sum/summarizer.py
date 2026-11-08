import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wav2sum.cloud import OpenAILLM
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

GROUNDING_RU = """\
Каждый фактический пункт, включая тему, должен содержать точную ссылку на исходную реплику в формате \
[U1 00:01–00:04], скопированную из входа. Вход может быть транскриптом или ранее составленными заметками; \
сжимай заметки, сохраняя ссылки на исходные реплики. Используй только ссылки из этого фрагмента или заметок. \
Только отдельные стандартные заголовки «Тема», «Обсуждения», «Решения», «Задачи», «Открытые вопросы» \
не требуют ссылки; заголовок раздела со своим названием должен содержать ссылку. Ссылки должны быть видимым \
обычным текстом, а не скрыты в комментариях или адресах ссылок. Не придумывай ссылки. \
Различай предложение и принятое решение: \
предложение само по себе не задача. Не приписывай исполнителя или срок, если это не сказано в указанных репликах. \
Учитывай явные исправления говорящего и не представляй исправлённое утверждение как окончательное.\
"""

GROUNDING_EN = """\
Every factual point, including the topic, must include an exact source utterance reference in the format \
[U1 00:01–00:04], copied from the input. The input can be transcript or earlier notes; condense notes while \
preserving references to original utterances. Use only references in this fragment or these notes. \
Only standalone standard section headings (Topic, Discussion, Decisions, Action items, Open questions) need no \
reference; a custom meeting section heading must have one. References must be visible plain text, not hidden in \
comments or link destinations. Do not invent references. Distinguish proposals from decisions: \
a proposal alone is not an action item. Do not assign an owner or deadline unless stated in the cited utterances. \
Respect explicit speaker corrections; do not present a corrected earlier statement as the final claim.\
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


SOURCE_LINE = re.compile(r"^\[(\d+:\d\d(?::\d\d)?) – (\d+:\d\d(?::\d\d)?)\] (.+)$", re.DOTALL)
CITATION = re.compile(r"\[U[1-9]\d* \d+:\d\d(?::\d\d)?–\d+:\d\d(?::\d\d)?\]")
STANDARD_HEADINGS = {
    "Тема",
    "Обсуждения",
    "Решения",
    "Задачи",
    "Открытые вопросы",
    "Topic",
    "Discussion",
    "Decisions",
    "Action items",
    "Open questions",
}


def _visible_text(text: str) -> str:
    # Remove comments across lines without joining the surrounding lines.
    text = re.sub(
        r"<!--.*?(?:-->|$)",
        lambda match: re.sub(r"[^\n]", " ", match.group()),
        text,
        flags=re.DOTALL,
    )
    # Markdown link destinations can contain balanced parentheses and escaped characters.
    chars = list(text)
    index = 0
    while index < len(text) - 1:
        if text[index : index + 2] != "](":
            index += 1
            continue
        end, depth = index + 2, 1
        while end < len(text) and depth:
            if text[end] == "\\" and end + 1 < len(text):
                end += 2
                continue
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
            end += 1
        if not depth:
            for pos in range(index + 1, end):
                if chars[pos] != "\n":
                    chars[pos] = " "
            index = end
        else:
            index += 1
    return re.sub(r"<[^>]*>", "", "".join(chars))


def _source_utterances(transcript: str) -> list[str]:
    utterances = []
    for index, utterance in enumerate(transcript.split("\n\n"), 1):
        match = SOURCE_LINE.fullmatch(utterance)
        if not match or not re.match(r"[^:\n]+: \S", match[3]):
            raise ValueError("Summary requires a timestamped, speaker-labelled transcript")
        start, end = match[1], match[2]
        for clock in (start, end):
            parts = [int(part) for part in clock.split(":")]
            if parts[-1] >= 60 or (len(parts) == 3 and parts[-2] >= 60):
                raise ValueError("Invalid utterance timestamp")
        if _seconds(start) > _seconds(end):
            raise ValueError("Utterance ends before it starts")
        utterances.append(f"[U{index} {start}–{end}] {match[3]}")
    return utterances


def _validated_reply(reply: str, available: set[str]) -> str:
    if not reply.strip():
        raise ValueError("Grounded summary is empty")
    # Validate every marker, including hidden ones, before deciding what is visible.
    all_refs = CITATION.findall(reply)
    if re.search(r"\[U", CITATION.sub("", reply), re.IGNORECASE):
        raise ValueError("Malformed source reference")
    if any(ref not in available for ref in all_refs):
        raise ValueError("Unknown or out-of-chunk source reference")
    cited = False
    for line in _visible_text(reply).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        refs = CITATION.findall(line)
        without_refs = CITATION.sub("", line)
        heading = re.fullmatch(r"(?:#{1,6}\s*)?\*\*([^*]+)\*\*\s*[:—–-]?", stripped)
        if heading and heading[1] in STANDARD_HEADINGS:
            continue
        if not refs or not re.sub(r"[\s\-–—*#•:]+", "", without_refs):
            raise ValueError("Factual summary line lacks a source reference")
        cited = True
    if not cited:
        raise ValueError("Grounded summary contains no cited facts")
    return reply.strip()


def _split_source(
    item: tuple[str, set[str]],
    llm: LLM | OpenAILLM,
    *,
    system: str,
    label: str,
    budget: int,
    max_tokens: int,
) -> list[tuple[str, set[str]]]:
    text, refs = item
    if llm.count_tokens(system) + llm.count_tokens(f"{label}:\n\n{text}") <= budget:
        return [item]
    # Every fragment retains the actual whole-utterance reference, not invented word timings.
    match = re.match(r"(\[U[^\]]+\] [^:\n]+: )(.*)", text, re.DOTALL)
    assert match is not None
    prefix, remaining = match.groups()

    def within_budget(value: str, limit: int) -> bool:
        return llm.count_tokens(system) + llm.count_tokens(f"{label}:\n\n{prefix}{value}") <= limit

    fragments = []
    while remaining:
        limit = budget if within_budget(remaining[:1], budget) else max_tokens
        low = next(
            (length for length in range(1, len(remaining) + 1) if within_budget(remaining[:length], limit)), None
        )
        if low is None:
            raise ValueError("Model context is too small for a source fragment")
        high = len(remaining)
        while low < high:
            middle = (low + high + 1) // 2
            if within_budget(remaining[:middle], limit):
                low = middle
            else:
                high = middle - 1
        end = low
        if end < len(remaining):
            boundary = end
            while boundary and not remaining[boundary - 1].isspace():
                boundary -= 1
            if boundary and within_budget(remaining[:boundary], limit):
                end = boundary
        fragments.append((prefix + remaining[:end], refs))
        remaining = remaining[end:]
    return fragments


def summarize(transcript: str, llm: LLM | OpenAILLM, terms: Sequence[str] = (), language: str = RU) -> str:
    if not transcript.strip():
        return ""
    prompts = PROMPTS[language]
    glossary = "\n\n" + prompts.glossary.format(", ".join(terms)) if terms else ""
    grounding = GROUNDING_RU if language == RU else GROUNDING_EN
    summary_system = prompts.summary + "\n\n" + grounding + glossary
    chunk_system = prompts.chunk + "\n\n" + grounding + glossary
    max_tokens = llm.num_ctx - RESERVED_TOKENS
    source = [(utterance, {utterance.split("]", 1)[0] + "]"}) for utterance in _source_utterances(transcript)]

    def user_text(items: list[tuple[str, set[str]]], label: str) -> str:
        return f"{label}:\n\n" + "\n\n".join(text for text, _ in items)

    def references(items: list[tuple[str, set[str]]]) -> set[str]:
        return set().union(*(refs for _, refs in items))

    def fits(system: str, user: str) -> bool:
        return llm.count_tokens(system) + llm.count_tokens(user) <= max_tokens

    def generate(system: str, user: str, available: set[str]) -> tuple[str, set[str]]:
        if not fits(system, user):
            raise ValueError("Grounded summary input exceeds the model context")
        # Both implementations add 64 to the growth limit; leave another 64 for chat framing.
        growth = (RESERVED_TOKENS - 128) / max(1, llm.count_tokens(user))
        reply = _validated_reply(llm.chat(system, user, max_growth=growth), available)
        return reply, set(CITATION.findall(_visible_text(reply)))

    source_user = user_text(source, prompts.transcript)
    if fits(summary_system, source_user):
        return generate(summary_system, source_user, references(source))[0]

    # Account for the actual system prompt, glossary, labels, and separators on every pass.
    overhead = max(llm.count_tokens(summary_system), llm.count_tokens(chunk_system))
    group_budget = max_tokens - max(1, (max_tokens - overhead) // 3)
    if group_budget <= overhead:
        raise ValueError("Model context is too small for grounded summary prompts")

    source = [
        fragment
        for item in source
        for fragment in _split_source(
            item, llm, system=chunk_system, label=prompts.transcript, budget=group_budget, max_tokens=max_tokens
        )
    ]

    def groups(items: list[tuple[str, set[str]]], label: str) -> list[list[tuple[str, set[str]]]]:
        batches: list[list[tuple[str, set[str]]]] = []
        current: list[tuple[str, set[str]]] = []
        for item in items:
            candidate = user_text([*current, item], label)
            if current and llm.count_tokens(chunk_system) + llm.count_tokens(candidate) > group_budget:
                batches.append(current)
                current = []
            current.append(item)
            if not fits(chunk_system, user_text(current, label)):
                raise ValueError("A source utterance or grounded note exceeds the model context")
        if current:
            batches.append(current)
        return batches

    batches = groups(source, prompts.transcript)
    logger.info("Long transcript (%d chars): summarizing %d parts first.", len(transcript), len(batches))
    notes = [generate(chunk_system, user_text(batch, prompts.transcript), references(batch)) for batch in batches]
    while True:
        notes_user = user_text(notes, prompts.notes)
        if fits(summary_system, notes_user):
            return generate(summary_system, notes_user, references(notes))[0]
        batches = groups(notes, prompts.notes)
        reduced = [generate(chunk_system, user_text(batch, prompts.notes), references(batch)) for batch in batches]
        if llm.count_tokens(user_text(reduced, prompts.notes)) >= llm.count_tokens(notes_user):
            raise ValueError("Grounded notes could not be reduced to fit the model context")
        notes = reduced


def chapters(transcript: str, llm: LLM | OpenAILLM, language: str = RU) -> str:
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
    except (ValueError, RuntimeError) as e:
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
