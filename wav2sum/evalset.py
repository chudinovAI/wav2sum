"""Dictation quality on a fixed set of phrases: WER and correctly written terms per recording condition.

Record each condition once (Enter starts and stops a phrase; phrases already recorded are skipped):
    uv run python -m wav2sum.evalset record quiet
Measure the current dictation pipeline (only reads the recordings):
    uv run python -m wav2sum.evalset
"""

import argparse
import re
import signal
import subprocess
from pathlib import Path

from wav2sum.audio import SAMPLE_RATE, load_audio

DIR = Path("~/wav2sum/evalset").expanduser()
# Quiet room, AirPods mic, noise from the Mac's speakers, half voice.
CONDITIONS = ["quiet", "airpods", "noise", "whisper"]
STYLE = "default"

# Text as it should be written, and the terms that must come out exactly so. A phrase's file is its number,
# so new phrases go only at the end.
PHRASES = [
    ("Запушь в main, я потом посмотрю.", ["main"]),
    ("Открой пулл реквест и добавь меня в ревьюеры.", []),
    ("Залей это на GitHub и скинь ссылку в Slack.", ["GitHub", "Slack"]),
    ("Тесты упали в CI, посмотри логи.", ["CI"]),
    ("Сделай ребейз на свежий main и разреши конфликты.", ["main"]),
    ("Задача в Linear уже в статусе In Review.", ["Linear", "In Review"]),
    ("Модель GigaAM распознаёт русскую речь, а Parakeet английскую.", ["GigaAM", "Parakeet"]),
    ("Поставь зависимость через uv и прогони pytest.", ["uv", "pytest"]),
    ("Давай перенесём созвон в Zoom на завтра после обеда.", ["Zoom"]),
    ("Я поднял Docker контейнер, но он падает при старте.", ["Docker"]),
    ("Кластер в Kubernetes опять не может скачать образ.", ["Kubernetes"]),
    ("Напиши мне в Telegram, когда освободишься.", ["Telegram"]),
    ("Сохрани заметку в Notion в папку с планами.", ["Notion"]),
    ("Проверь, что Python скрипт работает на macOS.", ["Python", "macOS"]),
    ("Надо обновить README и описать новые команды.", ["README"]),
    ("Саммари звонка получилось слишком длинным.", []),
    ("Мы используем Gemma через MLX прямо в процессе.", ["Gemma", "MLX"]),
    ("Сделай коммит и запушь ветку в origin.", ["origin"]),
    ("Фронтенд на React, бэкенд на FastAPI.", ["React", "FastAPI"]),
    ("Подними версию в pyproject и собери релиз.", ["pyproject"]),
    ("Задеплой на прод только после ревью.", []),
    ("Скинь мне JSON с ответом от API.", ["JSON", "API"]),
    ("Выгрузи данные в CSV и открой их в Excel.", ["CSV", "Excel"]),
    ("Я записал баг в Jira, посмотри, пожалуйста.", ["Jira"]),
    ("Таймаут на сокете стоит слишком маленький.", []),
    ("Привет, как дела? Давай вечером созвонимся.", []),
    ("Спасибо за встречу, отправлю итоги до конца дня.", []),
    ("Я сегодня работаю из дома, на связи после обеда.", []),
    ("Купи, пожалуйста, молоко и хлеб по дороге домой.", []),
    ("Напомни мне завтра утром позвонить в банк.", []),
    ("Мне кажется, стоит сначала поговорить с командой.", []),
    ("Встреча переносится, новое время пришлю позже.", []),
    ("Я не согласен с этим решением, давай обсудим.", []),
    ("Отправь договор юристу и попроси проверить сроки.", []),
    ("Хорошего вечера, до завтра.", []),
    ("Please review my pull request before the end of the day.", ["pull request"]),
    ("The build is failing on macOS but passes on Linux.", ["macOS", "Linux"]),
    ("Let's move the meeting to Thursday afternoon.", ["Thursday"]),
    ("I pushed a fix to the main branch an hour ago.", ["main"]),
    ("Can you share the Figma link in Slack?", ["Figma", "Slack"]),
    ("The latency of dictation is still too high.", ["latency"]),
    ("Thanks for the feedback, I will update the docs.", []),
    ("We need to rerun the benchmark with the new model.", ["benchmark"]),
    ("Деплой упал из-за того, что кончилось место на диске.", []),
    ("Открой VS Code и найди, где мы вызываем ffmpeg.", ["VS Code", "ffmpeg"]),
    ("Скажи Илье, что фича готова и ждёт мержа.", ["Илье"]),
]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m wav2sum.evalset", description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=DIR)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("record", help="read the phrases into the microphone").add_argument(
        "condition", choices=CONDITIONS
    )
    args = parser.parse_args(argv)
    if args.command == "record":
        record(args.dir / args.condition)
        return

    import mlx.core as mx

    from wav2sum.config import VOCABULARY_PATH, load_config
    from wav2sum.dictation import Dictator
    from wav2sum.models import Models
    from wav2sum.vocabulary import Vocabulary

    cfg = load_config()
    dictator = Dictator(Models(), cfg.dictation, vocabulary=Vocabulary(VOCABULARY_PATH), language=cfg.language)
    # The cleanup samples at the dictation temperature; a fixed seed per phrase gives the same numbers on a rerun.
    print(report(dictator, args.dir, seed=lambda: mx.random.seed(0)))


def record(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i, (text, _) in enumerate(PHRASES, 1):
        path = folder / f"{i:02}.wav"
        if path.exists():
            continue
        input(f"\n[{i}/{len(PHRASES)}] {text}\nEnter — начать")
        part = path.with_suffix(".part")
        # The default input, as the app records it: AirPods once they are connected.
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "avfoundation", "-i", ":default"]
        proc = subprocess.Popen([*cmd, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav", str(part)])
        input("● Enter — стоп")
        proc.send_signal(signal.SIGINT)
        if proc.wait() == 0:
            part.rename(path)


def report(dictator, folder: Path, seed=lambda: None) -> str:
    lines = [
        f"| {folder} | n | WER asr | WER clean | terms asr | terms clean |",
        "|---" * 6 + "|",
    ]
    for condition in CONDITIONS:
        recorded = [(p, folder / condition / f"{i:02}.wav") for i, p in enumerate(PHRASES, 1)]
        recorded = [(p, path) for p, path in recorded if path.exists()]
        if not recorded:
            continue
        words = edits_raw = edits_clean = terms = terms_raw = terms_clean = 0
        for (text, phrase_terms), path in recorded:
            raw, language = dictator.recognize(load_audio(path, channels=1)[:, 0])
            seed()
            clean = dictator.clean(raw, STYLE, language)
            words += len(_words(text))
            edits_raw += _edits(_words(text), _words(raw))
            edits_clean += _edits(_words(text), _words(clean))
            terms += len(phrase_terms)
            terms_raw += sum(_has(raw, term) for term in phrase_terms)
            terms_clean += sum(_has(clean, term) for term in phrase_terms)
        lines.append(
            f"| {condition} | {len(recorded)} | {edits_raw / words:.1%} | {edits_clean / words:.1%} | "
            f"{terms_raw}/{terms} | {terms_clean}/{terms} |"
        )
    return "\n".join(lines) + "\n"


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def _edits(reference: list[str], hypothesis: list[str]) -> int:
    row = list(range(len(hypothesis) + 1))
    for i, word in enumerate(reference, 1):
        previous, row[0] = row[0], i
        for j, other in enumerate(hypothesis, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (word != other))
    return row[-1]


def _has(text: str, term: str) -> bool:
    # A term counts in any case ("Main" for "main"), but only in the writing given: "мейн" is not "main".
    words, needle = _words(text), _words(term)
    return any(words[i : i + len(needle)] == needle for i in range(len(words)))


if __name__ == "__main__":
    main()
