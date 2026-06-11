from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from wav2sum.recorder import (
    DEFAULT_HEADPHONES,
    DEFAULT_INPUT_DEVICE,
    DEFAULT_OUTPUT_DEVICE,
    RecorderError,
    record,
)

# NB: wav2sum.pipeline (torch / transformers / pyannote) is imported lazily
# inside _process so that `wav2sum record` starts instantly without loading
# the heavy ML stack it doesn't need.

DEFAULT_MODEL = "gemma4:26b-a4b-it-q4_K_M"

# Calls are usually 1-on-1; use these unless --speaker overrides them.
DEFAULT_SPEAKER_NAMES = {0: "Я", 1: "Илья"}


def _parse_speaker_names(raw: list[str] | None) -> dict[int, str]:
    if not raw:
        return dict(DEFAULT_SPEAKER_NAMES)
    names: dict[int, str] = {}
    for item in raw:
        idx_str, name = item.split("=", 1)
        names[int(idx_str)] = name
    return names


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "record":
        _record_main(argv[1:])
    else:
        _summarize_main(argv)


# ── `wav2sum <audio>` : transcribe + summarize ───────────────

def _summarize_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="wav2sum",
        description="Аудио → транскрипт со спикерами → саммари  (подкоманда: record)",
    )
    parser.add_argument("audio", help="Путь к аудиофайлу (wav, mp3, ogg, …)")
    parser.add_argument("-o", "--output-dir", default="./output",
                        help="Директория для результатов (default: ./output)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Ollama-модель для саммаризации")
    parser.add_argument("--device", default=None,
                        help="Устройство: cuda / mps / cpu (авто, если не указано)")
    parser.add_argument("--speaker", action="append", metavar="IDX=ИМЯ",
                        help='Имя спикера, напр. --speaker "0=Алексей"')
    parser.add_argument("--num-speakers", type=int, default=2,
                        help="Точное число спикеров (default: 2; повышает точность диаризации)")
    parser.add_argument("--min-speakers", type=int, default=None,
                        help="Минимум спикеров (если точное число неизвестно)")
    parser.add_argument("--max-speakers", type=int, default=None,
                        help="Максимум спикеров (если точное число неизвестно)")
    parser.add_argument("--no-timestamps", action="store_true",
                        help="Убрать таймкоды из транскрипта")
    parser.add_argument("--no-cache", action="store_true",
                        help="Не использовать кэш диаризации/транскрипции")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Подробный вывод (debug)")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"Ошибка: файл не найден: {audio_path}", file=sys.stderr)
        sys.exit(1)

    # An explicit min/max range overrides the default fixed count.
    num_speakers = args.num_speakers
    if args.min_speakers is not None or args.max_speakers is not None:
        num_speakers = None

    _process(
        audio_path,
        output_dir=Path(args.output_dir),
        model=args.model,
        device=args.device,
        speaker_names=_parse_speaker_names(args.speaker),
        num_speakers=num_speakers,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        show_timestamps=not args.no_timestamps,
        use_cache=not args.no_cache,
    )


# ── `wav2sum record` : capture a call to wav ─────────────────

def _record_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="wav2sum record",
        description="Запись созвона (микрофон + звук из системы) → wav. Стоп — Ctrl-C.",
    )
    parser.add_argument("-o", "--output", default=None,
                        help="Путь для wav (default: ./recordings/<дата-время>.wav)")
    parser.add_argument("--input-device", default=DEFAULT_INPUT_DEVICE,
                        help=f"Aggregate-устройство записи (default: «{DEFAULT_INPUT_DEVICE}»)")
    parser.add_argument("--output-device", default=DEFAULT_OUTPUT_DEVICE,
                        help=f"Multi-Output для прослушки+захвата (default: «{DEFAULT_OUTPUT_DEVICE}»)")
    parser.add_argument("--headphones", default=DEFAULT_HEADPHONES,
                        help=f"Имя наушников для проверки подключения (default: «{DEFAULT_HEADPHONES}»)")
    parser.add_argument("--force", action="store_true",
                        help="Писать, даже если наушники не подключены")
    parser.add_argument("--summarize", action="store_true",
                        help="После записи сразу прогнать в транскрипт + саммари")
    # passthrough к пайплайну (только при --summarize)
    parser.add_argument("--output-dir", default="./output",
                        help="Куда писать результаты при --summarize (default: ./output)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama-модель для саммари")
    parser.add_argument("--num-speakers", type=int, default=2,
                        help="Число спикеров для диаризации (default: 2)")
    parser.add_argument("--speaker", action="append", metavar="IDX=ИМЯ",
                        help='Имя спикера, напр. --speaker "0=Алексей"')
    parser.add_argument("--device", default=None, help="cuda / mps / cpu")
    parser.add_argument("-v", "--verbose", action="store_true", help="Подробный вывод")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.output:
        out_path = Path(args.output)
    else:
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_path = Path("./recordings") / f"call-{stamp}.wav"

    try:
        wav = record(
            out_path,
            input_device=args.input_device,
            output_device=args.output_device,
            headphones=args.headphones,
            force=args.force,
        )
    except RecorderError as e:
        print(f"Ошибка записи: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nЗапись сохранена: {wav.resolve()}")

    if args.summarize:
        print("Прогоняю запись через пайплайн …\n")
        _process(
            wav,
            output_dir=Path(args.output_dir),
            model=args.model,
            device=args.device,
            speaker_names=_parse_speaker_names(args.speaker),
            num_speakers=args.num_speakers,
            min_speakers=None,
            max_speakers=None,
            show_timestamps=True,
            use_cache=True,
        )
    else:
        print(f"Готово к саммари:  wav2sum \"{wav}\" --num-speakers 2")


# ── shared: run pipeline + write artifacts ───────────────────

def _process(
    audio_path: Path,
    *,
    output_dir: Path,
    model: str,
    device: str | None,
    speaker_names: dict[int, str] | None,
    num_speakers: int | None,
    min_speakers: int | None,
    max_speakers: int | None,
    show_timestamps: bool,
    use_cache: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    from wav2sum.pipeline import Wav2SummaryPipeline  # heavy import, deferred

    pipeline = Wav2SummaryPipeline(ollama_model=model, device=device)
    result = pipeline.run(
        audio_path=audio_path,
        speaker_names=speaker_names,
        output_dir=output_dir,
        show_timestamps=show_timestamps,
        num_speakers=num_speakers,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        use_cache=use_cache,
    )

    _write_artifacts(result, output_dir, model)

    print("\n" + "=" * 60)
    print("ТРАНСКРИПТ")
    print("=" * 60)
    print(result.transcript_text)
    print("\n" + "=" * 60)
    print("САММАРИ")
    print("=" * 60)
    print(result.summary)
    print("\n" + "-" * 60)
    print(f"Файлы: {output_dir.resolve()}")


def _write_artifacts(result: PipelineResult, output_dir: Path, model: str) -> None:
    (output_dir / "transcript.txt").write_text(result.transcript_text, encoding="utf-8")
    (output_dir / "summary.md").write_text(result.summary, encoding="utf-8")
    (output_dir / "meta.json").write_text(
        json.dumps(
            {
                "audio": Path(result.audio_path).name,
                "duration_sec": round(result.duration_sec, 1),
                "num_speakers": len({d.speaker for d in result.diar_segments}),
                "num_segments": len(result.utterances),
                "model": model,
                "timings": {k: round(v, 2) for k, v in result.timings.items()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
