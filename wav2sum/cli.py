import argparse
import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from pathlib import Path

from wav2sum.capture import CallRecorder, CaptureError, Level, list_mics, new_recording_path
from wav2sum.config import CONFIG_PATH, Config, load_config

COMMANDS = ("transcribe", "record", "mics", "app", "tui", "serve", "status", "stop", "dictate")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "transcribe")

    cfg = load_config()
    args = _parser(cfg).parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.command is None:
        _parser(cfg).print_help()
    elif args.command == "mics":
        print(list_mics(), end="")
    elif args.command == "record":
        _record(args, cfg)
    elif args.command == "app":
        from wav2sum.build import install_app

        app = install_app()
        subprocess.run(["open", str(app)], check=True)
        print(f"Запущено: {app} (иконка в меню-баре)")
    elif args.command == "tui":
        from wav2sum.tui import Wav2SumTUI

        Wav2SumTUI(cfg).run()
    elif args.command == "serve":
        from wav2sum.daemon import serve

        serve(cfg, args.verbose)
    elif args.command in ("status", "stop"):
        _ask_daemon("status" if args.command == "status" else "shutdown")
    elif args.command == "dictate":
        _dictate(args, cfg)
    else:
        if not args.audio.exists():
            sys.exit(f"Файл не найден: {args.audio}")
        _process(args.audio, args, cfg, layout=None if args.layout == "auto" else args.layout)


def _parser(cfg: Config) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav2sum",
        description="Локальные созвоны: запись → транскрипт → саммари.",
        epilog=f"Настройки: {CONFIG_PATH}",
    )
    commands = parser.add_subparsers(dest="command")

    transcribe = commands.add_parser("transcribe", help="транскрипт + саммари записи (можно без слова transcribe)")
    transcribe.add_argument("audio", type=Path, help="аудиофайл (wav, mp3, m4a, …)")
    transcribe.add_argument(
        "--layout",
        choices=["auto", "call", "mono"],
        default="auto",
        help="call — стерео из `wav2sum record` (L=я, R=собеседник); auto — по .json рядом",
    )
    _add_processing_args(transcribe, cfg)

    record = commands.add_parser("record", help="записать созвон: микрофон + системный звук (стоп — Ctrl-C)")
    record.add_argument(
        "-o", "--output", type=Path, help=f"путь для wav (default: {cfg.recordings_dir}/call-<время>.wav)"
    )
    record.add_argument("--mic", default=cfg.mic, help="имя или UID микрофона (список: wav2sum mics)")
    record.add_argument("--summarize", action="store_true", help="после записи сразу транскрипт + саммари")
    _add_processing_args(record, cfg)

    commands.add_parser("mics", help="список микрофонов")

    commands.add_parser("tui", help="терминальный интерфейс: созвоны, саммари, история диктовок")
    commands.add_parser("app", help="собрать и запустить приложение в меню-баре (~/Applications/Wav2Sum.app)")
    serve = commands.add_parser(
        "serve", help="фоновый процесс: модели в памяти, диктовка, запись (обычно его запускает меню-бар)"
    )
    serve.add_argument("-v", "--verbose", action="store_true")
    commands.add_parser("status", help="состояние фонового процесса")
    commands.add_parser("stop", help="остановить фоновый процесс")

    dictate = commands.add_parser("dictate", help="прогнать аудиофайл через диктовку (для отладки)")
    dictate.add_argument("audio", type=Path)
    dictate.add_argument("--app", help="bundle id приложения, напр. ru.keepcoder.Telegram")
    dictate.add_argument("--title", help="заголовок окна (для браузеров)")
    dictate.add_argument(
        "--command", dest="command_mode", action="store_true", help="голосовая команда над --selection"
    )
    dictate.add_argument("--selection", help="выделенный текст для --command")
    return parser


def _add_processing_args(parser: argparse.ArgumentParser, cfg: Config) -> None:
    parser.add_argument(
        "--output-dir", type=Path, default=cfg.output_dir, help=f"куда писать (default: {cfg.output_dir})"
    )
    parser.add_argument("--model", default=cfg.model, help=f"Ollama-модель для саммари (default: {cfg.model})")
    parser.add_argument("--no-summary", action="store_true", help="только транскрипт")
    parser.add_argument("--no-cache", action="store_true", help="не брать транскрипцию из кэша")
    parser.add_argument("--device", help="cuda / mps / cpu (default: авто)")
    parser.add_argument("--me", default=cfg.me, help=f"моё имя в созвоне (default: {cfg.me})")
    parser.add_argument("--them", default=cfg.them, help=f"собеседник 1-на-1 (default: {cfg.them})")
    parser.add_argument(
        "--speaker",
        action="append",
        default=[],
        metavar="N=ИМЯ",
        help='имя диаризованного спикера N (с нуля), напр. --speaker "0=Алексей"',
    )
    parser.add_argument("--num-speakers", type=int, default=2, help="сколько всего людей, включая меня (default: 2)")
    parser.add_argument("--min-speakers", type=int, help="минимум людей, если точно неизвестно")
    parser.add_argument("--max-speakers", type=int, help="максимум людей, если точно неизвестно")
    parser.add_argument("-v", "--verbose", action="store_true")


def _record(args, cfg: Config) -> None:
    recorder = CallRecorder(args.output or new_recording_path(cfg.recordings_dir), mic=args.mic, on_level=_meter)
    try:
        recorder.start()
        with contextlib.suppress(KeyboardInterrupt):
            recorder.wait()
        wav = recorder.stop()
    except CaptureError as e:
        sys.exit(f"\nОшибка записи: {e}")

    print(f"\nЗапись: {wav}")
    if args.summarize:
        _process(wav, args, cfg, layout="call")
    else:
        print(f'Саммари: wav2sum "{wav}"')


def _process(audio: Path, args, cfg: Config, layout: str | None) -> None:
    from wav2sum.calls import Names, Speakers, process
    from wav2sum.llm import LLM
    from wav2sum.models import Models

    ranged = args.min_speakers is not None or args.max_speakers is not None
    result = process(
        audio,
        Models(args.device),
        args.output_dir,
        llm=None if args.no_summary else LLM(args.model, num_ctx=cfg.num_ctx),
        layout=layout,
        speakers=Speakers(None if ranged else args.num_speakers, args.min_speakers, args.max_speakers),
        names=Names(args.me, args.them, {int(n): name for n, name in (s.split("=", 1) for s in args.speaker)}),
        use_cache=not args.no_cache,
        on_stage=lambda stage: logging.info("%s …", stage),
    )
    print(f"\n{result.transcript}")
    if result.summary:
        print(f"\n{'─' * 60}\n{result.summary}")
    print(f"\n{'─' * 60}\nФайлы: {result.out_dir}")


def _ask_daemon(cmd: str) -> None:
    from wav2sum.client import DaemonClient, DaemonError

    async def ask():
        client = await DaemonClient.connect()
        try:
            return await client.request(cmd)
        finally:
            await client.close()

    try:
        print(json.dumps(asyncio.run(ask()), ensure_ascii=False, indent=2))
    except DaemonError as e:
        sys.exit(str(e))


def _dictate(args, cfg: Config) -> None:
    from wav2sum.audio import load_audio
    from wav2sum.client import DaemonClient, DaemonError

    audio = load_audio(args.audio, channels=1)[:, 0]
    context = {"app": args.app, "title": args.title, "command": args.command_mode, "selection": args.selection}

    async def via_daemon():
        client = await DaemonClient.connect()
        try:
            return await client.dictate(audio, **context)
        finally:
            await client.close()

    try:
        result = asyncio.run(via_daemon())
    except DaemonError:
        from dataclasses import asdict

        from wav2sum.dictation import Dictator
        from wav2sum.models import Models

        result = asdict(Dictator(Models(), cfg.dictation).dictate(audio, **context))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _meter(level: Level) -> None:
    if not sys.stderr.isatty():
        return
    m, s = divmod(int(level.seconds), 60)
    sys.stderr.write(
        f"\r\033[31m●\033[0m {m:02d}:{s:02d}   я {_bar(level.mic_db)}   они {_bar(level.sys_db)}   Ctrl-C — стоп "
    )
    sys.stderr.flush()


def _bar(db: float, width: int = 12) -> str:
    filled = max(0, min(width, round((db + 60) / 60 * width)))
    return "▮" * filled + "▯" * (width - filled)
