from __future__ import annotations

import logging
import re
import signal
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Defaults match the devices set up in Audio MIDI Setup (see README).
DEFAULT_INPUT_DEVICE = "Call recorder"   # Aggregate: MacBook mic + BlackHole
DEFAULT_OUTPUT_DEVICE = "Call output"    # Multi-Output: headphones + BlackHole
DEFAULT_HEADPHONES = "WH-1000XM5"


class RecorderError(RuntimeError):
    """Setup is wrong (missing device, headphones off, …)."""


def record(
    out_path: str | Path,
    input_device: str = DEFAULT_INPUT_DEVICE,
    output_device: str = DEFAULT_OUTPUT_DEVICE,
    headphones: str | None = DEFAULT_HEADPHONES,
    force: bool = False,
) -> Path:
    """Route system audio through BlackHole and record mic + system to a wav.

    Saves the current output device, switches to ``output_device`` so the call
    audio is both heard and captured, records until Ctrl-C, then restores the
    previous output — whatever it was — on the way out.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    outputs = _available_outputs()
    if output_device not in outputs:
        raise RecorderError(
            f"Устройство вывода «{output_device}» не найдено. "
            f"Создай Multi-Output в Audio MIDI Setup. Доступно: {', '.join(outputs)}"
        )
    if headphones and not force and headphones not in outputs:
        raise RecorderError(
            f"Наушники «{headphones}» не подключены — записывать нечего "
            f"(сторона собеседника уйдёт в никуда). Подключи их или запусти с --force."
        )

    idx = _avfoundation_audio_index(input_device)
    if idx is None:
        raise RecorderError(
            f"Устройство записи «{input_device}» не найдено среди avfoundation-входов. "
            f"Создай Aggregate Device (микрофон + BlackHole) в Audio MIDI Setup."
        )

    previous = _current_output()
    logger.info("Текущий вывод «%s» → переключаю на «%s» на время записи.", previous, output_device)
    _set_output(output_device)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-f", "avfoundation", "-i", f":{idx}",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-y", str(out_path),
    ]
    logger.info("Запись началась → %s.  Останови по Ctrl-C.", out_path)

    proc = subprocess.Popen(cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        # Tell ffmpeg to stop gracefully so it finalizes the wav header.
        proc.send_signal(signal.SIGINT)
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
        if previous and previous != output_device:
            logger.info("Возвращаю вывод на «%s».", previous)
            _set_output(previous)

    logger.info("Запись сохранена: %s", out_path)
    return out_path


# ── device helpers (SwitchAudioSource + ffmpeg) ──────────────

def _current_output() -> str:
    return _run(["SwitchAudioSource", "-c", "-t", "output"]).strip()


def _available_outputs() -> list[str]:
    return [ln.strip() for ln in _run(["SwitchAudioSource", "-a", "-t", "output"]).splitlines() if ln.strip()]


def _set_output(name: str) -> None:
    subprocess.run(["SwitchAudioSource", "-s", name, "-t", "output"], capture_output=True, text=True)


def _avfoundation_audio_index(name: str) -> int | None:
    """Find the avfoundation audio device index by name (matched on stderr)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    in_audio = False
    for line in proc.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m and m.group(2) == name:
            return int(m.group(1))
    return None


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RecorderError(f"Команда {cmd[0]} упала: {result.stderr.strip()}")
    return result.stdout
