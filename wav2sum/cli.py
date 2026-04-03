from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from wav2sum.pipeline import Wav2SummaryPipeline


def _parse_speaker_names(raw: list[str] | None) -> dict[int, str] | None:
    if not raw:
        return None
    names: dict[int, str] = {}
    for item in raw:
        idx_str, name = item.split("=", 1)
        names[int(idx_str)] = name
    return names


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wav2sum",
        description="Аудио → транскрипт со спикерами → саммари",
    )
    parser.add_argument("audio", help="Путь к аудиофайлу (wav, mp3, ogg, …)")
    parser.add_argument(
        "-o", "--output-dir",
        default="./output",
        help="Директория для результатов (default: ./output)",
    )
    parser.add_argument(
        "--model",
        default="gemma4:26b-a4b-it-q4_K_M",
        help="Ollama-модель для саммаризации",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Устройство: cuda / mps / cpu (авто, если не указано)",
    )
    parser.add_argument(
        "--speaker",
        action="append",
        metavar="IDX=ИМЯ",
        help='Имя спикера, напр. --speaker "0=Алексей"',
    )
    parser.add_argument(
        "--no-timestamps",
        action="store_true",
        help="Убрать таймкоды из транскрипта",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Подробный вывод (debug)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"Ошибка: файл не найден: {audio_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    speaker_names = _parse_speaker_names(args.speaker)

    pipeline = Wav2SummaryPipeline(
        ollama_model=args.model,
        device=args.device,
    )

    result = pipeline.run(
        audio_path=audio_path,
        speaker_names=speaker_names,
        output_dir=output_dir,
        show_timestamps=not args.no_timestamps,
    )

    transcript_path = output_dir / "transcript.txt"
    transcript_path.write_text(result.transcript_text, encoding="utf-8")

    summary_path = output_dir / "summary.md"
    summary_path.write_text(result.summary, encoding="utf-8")

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
