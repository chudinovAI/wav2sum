import argparse
import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from pathlib import Path

from wav2sum.capture import CallRecorder, CaptureError, Level, list_mics, new_recording_path
from wav2sum.config import CONFIG_PATH, STATE_DIR, Config, exclusive, load_config

COMMANDS = ("transcribe", "record", "mics", "app", "tui", "serve", "status", "stop", "dictate", "cloud-keys", "setup")


def _arguments() -> tuple[Config, argparse.Namespace]:
    argv = sys.argv[1:]
    first = 1 if argv and argv[0] == "--cloud" else 0
    if len(argv) > first and argv[first] not in COMMANDS and not argv[first].startswith("-"):
        argv.insert(first, "transcribe")

    cfg = Config() if len(argv) > first and argv[first] in {"cloud-keys", "setup"} else load_config()
    args = _parser(cfg).parse_args(argv)
    cfg.cloud = args.cloud
    return cfg, args


def main() -> None:
    cfg, args = _arguments()
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
    elif args.command in {"cloud-keys", "setup"}:
        _setup_command(args.command)
    elif args.command == "app":
        _app(args, cfg)
    elif args.command == "tui":
        from wav2sum.tui import Wav2SumTUI

        Wav2SumTUI(cfg).run()
    elif args.command == "serve":
        with exclusive(STATE_DIR / "daemon.lock", wait=False) as locked:
            if not locked:
                sys.exit("wav2sum daemon is already running")
            from wav2sum.daemon import serve

            serve(cfg, args.verbose)
    elif args.command in ("status", "stop"):
        _ask_daemon("status" if args.command == "status" else "shutdown", cfg)
    elif args.command == "dictate":
        _dictate(args, cfg)
    else:
        if not args.audio.exists():
            sys.exit(f"File not found: {args.audio}")
        _process(args.audio, args, cfg, layout=None if args.layout == "auto" else args.layout)


def _app(args, cfg: Config) -> None:
    if args.watch:
        from wav2sum.devwatch import run

        run(cloud=cfg.cloud)
    else:
        from wav2sum.build import install_app

        app = install_app()
        subprocess.run(["open", str(app), *(["--args", "--cloud"] if cfg.cloud else [])], check=True)
        print(f"Started: {app} (menu bar icon)")


def _parser(cfg: Config) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav2sum",
        description="Transcribe calls locally, or use cloud transcription and summaries with --cloud.",
        epilog=f"Settings: {CONFIG_PATH}",
    )
    parser.add_argument("--cloud", action="store_true", help="explicitly use cloud providers")
    commands = parser.add_subparsers(dest="command")

    transcribe = commands.add_parser("transcribe", help="transcribe a recording (the default command)")
    transcribe.add_argument("audio", type=Path, help="audio file (wav, mp3, m4a, …)")
    transcribe.add_argument(
        "--layout",
        choices=["auto", "call", "mono"],
        default="auto",
        help="call — stereo from `wav2sum record` (L=me, R=them); auto — from the .json next to it",
    )
    _add_processing_args(transcribe, cfg)

    record = commands.add_parser("record", help="record a call: microphone + system audio (Ctrl-C to stop)")
    record.add_argument("-o", "--output", type=Path, help=f"wav path (default: {cfg.recordings_dir}/call-<time>.wav)")
    record.add_argument("--mic", default=cfg.mic, help="microphone name or UID (list: wav2sum mics)")
    record.add_argument(
        "--summarize", action="store_true", help="process right after recording (summary in cloud mode)"
    )
    _add_processing_args(record, cfg)

    commands.add_parser("mics", help="list microphones")

    commands.add_parser("tui", help="terminal UI: calls and dictation history")
    app = commands.add_parser("app", help="build and launch the menu bar app (~/Applications/Wav2Sum.app)")
    app.add_argument("--watch", action="store_true", help="watch Swift sources and safely reload the development app")
    serve = commands.add_parser(
        "serve", help="background process: models in memory, dictation, recording (usually started by the menu bar app)"
    )
    serve.add_argument("-v", "--verbose", action="store_true")
    commands.add_parser("status", help="background process status")
    commands.add_parser("stop", help="stop the background process")
    commands.add_parser("cloud-keys", help="save Deepgram/OpenAI keys in macOS Keychain (hidden input)")
    commands.add_parser("setup", help="local app setup bridge (JSON on stdin/stdout)")

    dictate = commands.add_parser("dictate", help="run an audio file through dictation (for debugging)")
    dictate.add_argument("audio", type=Path)
    dictate.add_argument("--app", help="app bundle id, e.g. ru.keepcoder.Telegram")
    dictate.add_argument("--title", help="window title (for browsers)")
    dictate.add_argument(
        "--command", dest="command_mode", action="store_true", help="voice command applied to --selection"
    )
    dictate.add_argument("--selection", help="selected text for --command")
    for subparser in commands.choices.values():
        subparser.add_argument("--cloud", action="store_true", default=argparse.SUPPRESS, help="use cloud providers")
    return parser


def _setup_command(command: str) -> None:
    if command == "setup":
        from wav2sum.settings import main as setup

        setup()
        return
    from wav2sum.credentials import configure

    try:
        configure()
    except (EOFError, KeyboardInterrupt):
        sys.exit("Cloud key setup cancelled")
    except (RuntimeError, ValueError) as exc:
        sys.exit(str(exc))


def _add_processing_args(parser: argparse.ArgumentParser, cfg: Config) -> None:
    parser.add_argument(
        "--output-dir", type=Path, default=cfg.output_dir, help=f"where to write results (default: {cfg.output_dir})"
    )
    parser.add_argument(
        "--model", default=cfg.summary_model, help=f"OpenAI summary model for cloud mode (default: {cfg.summary_model})"
    )
    parser.add_argument("--no-summary", action="store_true", help="transcript only")
    parser.add_argument("--no-cache", action="store_true", help="ignore the cached transcription")
    parser.add_argument("--device", help="cuda / mps / cpu (default: auto)")
    parser.add_argument(
        "--language",
        choices=["auto", "ru", "en"],
        default=cfg.language,
        help=f"speech language (default: {cfg.language})",
    )
    parser.add_argument("--me", default=cfg.me, help=f"my name in the transcript (default: {cfg.me})")
    parser.add_argument("--them", default=cfg.them, help=f"the other person in a 1-on-1 (default: {cfg.them})")
    parser.add_argument(
        "--speaker",
        action="append",
        default=[],
        metavar="N=NAME",
        help='name of diarized speaker N (zero-based), e.g. --speaker "0=Алексей"',
    )
    parser.add_argument("--num-speakers", type=int, default=2, help="total number of people, including me (default: 2)")
    parser.add_argument("--min-speakers", type=int, help="minimum number of people, if unsure")
    parser.add_argument("--max-speakers", type=int, help="maximum number of people, if unsure")
    parser.add_argument("-v", "--verbose", action="store_true")


def _record(args, cfg: Config) -> None:
    recorder = CallRecorder(args.output or new_recording_path(cfg.recordings_dir), mic=args.mic, on_level=_meter)
    try:
        recorder.start()
        with contextlib.suppress(KeyboardInterrupt):
            recorder.wait()
        wav = recorder.stop()
    except CaptureError as e:
        sys.exit(f"\nRecording failed: {e}")

    print(f"\nRecording: {wav}")
    if args.summarize:
        _process(wav, args, cfg, layout="call")
    else:
        print(f'Process: wav2sum {"--cloud " if cfg.cloud else ""}"{wav}"')


def _process(audio: Path, args, cfg: Config, layout: str | None) -> None:
    from wav2sum.calls import Names, Speakers, process
    from wav2sum.config import VOCABULARY_PATH
    from wav2sum.models import Models
    from wav2sum.vocabulary import Vocabulary

    ranged = args.min_speakers is not None or args.max_speakers is not None
    llm = _summary_llm(args, cfg)
    try:
        result = process(
            audio,
            Models(
                args.device,
                cloud=cfg.cloud,
                call_model=cfg.call_model,
                allow_unsafe_nemotron=cfg.allow_unsafe_nemotron,
            ),
            args.output_dir,
            llm=llm,
            layout=layout,
            speakers=Speakers(None if ranged else args.num_speakers, args.min_speakers, args.max_speakers),
            names=Names(args.me, args.them, {int(n): name for n, name in (s.split("=", 1) for s in args.speaker)}),
            language=args.language,
            vocabulary=Vocabulary(VOCABULARY_PATH),
            dictionary=cfg.dictation.dictionary,
            use_cache=not args.no_cache,
            on_stage=lambda stage: logging.info("%s …", stage),
        )
    finally:
        if llm is not None:
            llm.unload()
    print(f"\n{result.transcript}")
    if result.summary:
        print(f"\n{'─' * 60}\n{result.summary}")
    print(f"\n{'─' * 60}\nFiles: {result.out_dir}")


def _summary_llm(args, cfg: Config):
    if not cfg.cloud or args.no_summary:
        return None
    from wav2sum.cloud import OpenAILLM

    return OpenAILLM(args.model, num_ctx=cfg.num_ctx)


def _ask_daemon(cmd: str, cfg: Config) -> None:
    from wav2sum.client import DaemonClient, DaemonError

    async def ask():
        client = await DaemonClient.connect(cloud=cfg.cloud)
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
    from wav2sum.client import DaemonClient, DaemonUnavailable

    audio = load_audio(args.audio, channels=1)[:, 0]
    context = {"app": args.app, "title": args.title, "command": args.command_mode, "selection": args.selection}

    async def via_daemon():
        try:
            client = await DaemonClient.connect(cloud=cfg.cloud)
        except DaemonUnavailable:
            return None
        try:
            return await client.dictate(audio, **context)
        finally:
            await client.close()

    result = asyncio.run(via_daemon())
    if result is None:
        from dataclasses import asdict

        from wav2sum.config import VOCABULARY_PATH
        from wav2sum.dictation import Dictator
        from wav2sum.models import Models
        from wav2sum.vocabulary import Vocabulary

        dictator = Dictator(
            Models(cloud=cfg.cloud, call_model=cfg.call_model, allow_unsafe_nemotron=cfg.allow_unsafe_nemotron),
            cfg.dictation,
            vocabulary=Vocabulary(VOCABULARY_PATH),
            language=cfg.language,
        )
        try:
            result = asdict(dictator.dictate(audio, **context))
        finally:
            if cfg.cloud:
                dictator.llm.unload()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _meter(level: Level) -> None:
    if not sys.stderr.isatty():
        return
    m, s = divmod(int(level.seconds), 60)
    sys.stderr.write(
        f"\r\033[31m●\033[0m {m:02d}:{s:02d}   me {_bar(level.mic_db)}   them {_bar(level.sys_db)}   Ctrl-C to stop "
    )
    sys.stderr.flush()


def _bar(db: float, width: int = 12) -> str:
    filled = max(0, min(width, round((db + 60) / 60 * width)))
    return "▮" * filled + "▯" * (width - filled)
