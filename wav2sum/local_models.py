"""Privacy-preserving hardware profiles and explicit local-model preparation."""

import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

QWEN_MODEL = "Qwen/Qwen3-ASR-1.7B-hf"
QWEN_REVISION = "bcd2b5b7f32b480ab5790554cfa8347f246a14f3"
CLEANUP_MODEL = "mlx-community/gemma-4-E4B-it-qat-4bit"
CLEANUP_REVISION = "0f35c6f6d386f7f74e628bd7c6526ce531212300"
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
DIARIZATION_REVISION = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"
NEMOTRON_MODEL = "sakasegawa/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-GGUF"
NEMOTRON_REVISION = "55d289085ed6b1acd1cfd2e6cf279fdeb32fee5f"
NEMOTRON_FILE = "NVIDIA-Nemotron-3.5-Transcribe-30B-A4B-IQ4_NL.gguf"
NEMOTRON_SHA256 = "9688cee08c96ee0f54c0cc1cb07856307a79097fb0088cdb8fb31de6dd26c2be"
SPEECH_ASSET = "speech-0.9.4-macos-arm64-metal.zip"
SPEECH_SHA256 = "7ed31c27556926297c3ad521de289d97e17a17108feacb57a88d4b52444e6703"
QWEN_FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "processor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
)
CLEANUP_FILES = (
    "config.json",
    "generation_config.json",
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
    "model.safetensors.index.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
)
DIARIZATION_FILES = (
    "config.yaml",
    "embedding/pytorch_model.bin",
    "segmentation/pytorch_model.bin",
    "plda/plda.npz",
    "plda/xvec_transform.npz",
)
SPEECH_URL = f"https://github.com/nyosegawa/speech.cpp/releases/download/v0.9.4/{SPEECH_ASSET}"


def _sysctl(name):
    try:
        return subprocess.check_output(["sysctl", "-n", name], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def hardware_info():
    memory = _sysctl("hw.memsize") if platform.system() == "Darwin" else None
    try:
        memory_gib = round(int(memory) / 2**30, 1) if memory else None
    except ValueError:
        memory_gib = None
    try:
        disk = round(shutil.disk_usage(Path.home()).free / 2**30, 1)
    except OSError:
        disk = None
    return {
        "chip": _sysctl("machdep.cpu.brand_string") or "unknown",
        "memory_gib": memory_gib,
        "free_disk_gib": disk,
        "platform": platform.system(),
        "arch": platform.machine(),
    }


def recommendation(hardware=None):
    info = hardware if hardware is not None else hardware_info()
    memory = info.get("memory_gib")
    ram_class = next((size for size in (48, 32, 24, 16) if memory is not None and memory >= size), None)
    call_model = "nemotron" if ram_class == 48 else "qwen"
    return {
        "call_model": call_model,
        "cleanup_model": CLEANUP_MODEL,
        "ram_class_gib": ram_class,
        "qualification": "provisional",
        "reason": (
            "RAM-class guidance, not a measured speed or safety guarantee; "
            "Qwen is the lighter candidate. Nemotron uses about 19 GB for weights alone. "
            "Benchmark on this machine; close other workloads and watch swap."
        ),
    }


def check_memory(call_model, *, hardware=None, allow_unsafe=False):
    if call_model not in ("qwen", "nemotron"):
        raise ValueError("Unknown local call model")
    memory = (hardware if hardware is not None else hardware_info()).get("memory_gib")
    if call_model == "nemotron" and (memory is None or memory < 32) and not allow_unsafe:
        raise RuntimeError("Nemotron needs substantial memory; choose Qwen or explicitly override the memory warning.")


def model_root():
    return Path.home() / ".local/share/wav2sum/models"


def speech_paths():
    root = model_root()
    legacy = Path.home() / ".local/share/wav2sum-nemotron-study"
    new_bin = root / "speech-0.9.4" / "speech"
    new_model = root / "nemotron-IQ4_NL.gguf"
    binary = (
        Path(os.environ["WAV2SUM_SPEECH_BIN"]).expanduser()
        if "WAV2SUM_SPEECH_BIN" in os.environ
        else (new_bin if new_bin.is_file() else Path(shutil.which("speech") or legacy / "runtime-0.9.4/speech"))
    )
    model = (
        Path(os.environ["WAV2SUM_NEMOTRON_MODEL"]).expanduser()
        if "WAV2SUM_NEMOTRON_MODEL" in os.environ
        else (new_model if new_model.is_file() else legacy / "nemotron-IQ4_NL.gguf")
    )
    return binary, model


def _snapshot(repo, revision, **kwargs):
    return snapshot_download(repo, revision=revision, **kwargs)


def snapshot_path(repo, revision, required):
    try:
        path = Path(_snapshot(repo, revision, local_files_only=True, token=False))
        return path if all((path / name).is_file() for name in required) else None
    except (OSError, ValueError):
        return None


def model_status(call_model, cleanup_model, calls_enabled=True):
    if call_model not in ("qwen", "nemotron") or cleanup_model != CLEANUP_MODEL:
        raise ValueError("Unknown local model selection")
    qwen = snapshot_path(QWEN_MODEL, QWEN_REVISION, QWEN_FILES)
    cleanup = snapshot_path(CLEANUP_MODEL, CLEANUP_REVISION, CLEANUP_FILES)
    diarization = snapshot_path(DIARIZATION_MODEL, DIARIZATION_REVISION, DIARIZATION_FILES) if calls_enabled else None
    nemotron = False
    if calls_enabled:
        binary, model = speech_paths()
        nemotron = binary.is_file() and model.is_file()
    return {
        "qwen": bool(qwen),
        "cleanup": bool(cleanup),
        "nemotron": nemotron,
        "diarization": bool(diarization),
        "diarization_note": (
            "Optional for mono or multi-speaker calls: accept the pyannote terms on Hugging Face, "
            "then prepare with a token."
        ),
    }


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_speech():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Automatic speech.cpp preparation supports macOS arm64 only")
    root = model_root()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        archive = Path(tmp) / SPEECH_ASSET
        with urllib.request.urlopen(SPEECH_URL, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        if _sha256(archive) != SPEECH_SHA256:
            raise RuntimeError("speech.cpp release checksum mismatch")
        # Extract only ordinary flat files; do not trust archive paths or symlinks.
        runtime = Path(tmp) / "runtime"
        runtime.mkdir()
        with zipfile.ZipFile(archive) as zipped:
            for info in zipped.infolist():
                name = Path(info.filename)
                if info.is_dir():
                    continue
                if name.is_absolute() or ".." in name.parts or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("Unsafe speech.cpp release archive")
                dest = runtime / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                dest.chmod((info.external_attr >> 16) & 0o777 or 0o644)
        matches = list(runtime.rglob("speech"))
        if len(matches) != 1 or not matches[0].is_file():
            raise RuntimeError("speech.cpp archive has no unique executable")
        # Archives may wrap the distribution in a directory; preserve its sibling libraries.
        installed = root / "speech-0.9.4"
        if not installed.exists():
            matches[0].chmod(matches[0].stat().st_mode | 0o111)
            shutil.move(str(matches[0].parent), str(installed))


def prepare_models(call_model, cleanup_model, hf_token=None, *, calls_enabled=True):
    if call_model not in ("qwen", "nemotron") or cleanup_model != CLEANUP_MODEL:
        raise ValueError("Unknown local model selection")
    try:
        _snapshot(QWEN_MODEL, QWEN_REVISION, token=False)
        _snapshot(CLEANUP_MODEL, CLEANUP_REVISION, token=False)
        if calls_enabled and call_model == "nemotron":
            _prepare_speech()
            source = Path(hf_hub_download(NEMOTRON_MODEL, NEMOTRON_FILE, revision=NEMOTRON_REVISION, token=False))
            if _sha256(source) != NEMOTRON_SHA256:
                raise RuntimeError("Nemotron model checksum mismatch")
            destination = model_root() / "nemotron-IQ4_NL.gguf"
            if not destination.exists():
                os.symlink(source, destination)
        if calls_enabled and hf_token:
            try:
                _snapshot(DIARIZATION_MODEL, DIARIZATION_REVISION, token=hf_token)
            except Exception:
                raise RuntimeError(
                    "Could not prepare pyannote. Accept its Hugging Face terms separately and provide a valid token."
                ) from None
    except Exception as exc:
        if hf_token and hf_token in str(exc):
            raise RuntimeError("Model preparation failed; check network access and Hugging Face access.") from None
        raise
    return model_status(call_model, cleanup_model) if calls_enabled else model_status(call_model, cleanup_model, False)
