from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


def ensure_wav_16k_mono(audio_path: str | Path, output_dir: str | Path | None = None) -> Path:
    """Convert any audio to 16 kHz mono WAV via ffmpeg."""
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="wav2sum_"))
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / f"{audio_path.stem}_16k.wav"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(output_path),
    ]

    logger.info("Converting %s → %s", audio_path.name, output_path.name)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (code {result.returncode}):\n{result.stderr}")

    return output_path


def get_audio_duration(audio_path: str | Path) -> float:
    """Return duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr}")
    return float(result.stdout.strip())
