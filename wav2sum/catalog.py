import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CallEntry:
    path: Path
    audio: Path | None
    meta: dict | None


def _children(directory: Path) -> list[Path]:
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return []
    with entries:
        return [directory / entry.name for entry in entries]


def list_calls(recordings_dir: Path, output_dir: Path) -> list[CallEntry]:
    entries = []
    processed_audio = set()
    for folder in _children(output_dir):
        meta_path = folder / "meta.json"
        try:
            meta_path.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        audio = Path(meta["audio"]).resolve() if meta.get("audio") else None
        entries.append(CallEntry(path=folder, audio=audio, meta=meta))
        if audio is not None:
            processed_audio.add(audio)

    for audio in _children(recordings_dir):
        if audio.match("*.wav"):
            resolved = audio.resolve()
            if resolved not in processed_audio:
                entries.append(CallEntry(path=audio, audio=resolved, meta=None))

    return sorted(entries, key=lambda entry: entry.path.stat().st_mtime, reverse=True)
