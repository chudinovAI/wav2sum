import json
import shutil
import subprocess
from pathlib import Path

from wav2sum.audio import file_hash
from wav2sum.config import HISTORY_PATH, exclusive

CAN_TRASH = shutil.which("trash") is not None


def load_dictations(path: Path = HISTORY_PATH, limit: int = 500) -> list[dict]:
    """Newest first."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in reversed(lines) if line]


def append_dictation(entry: dict, path: Path = HISTORY_PATH) -> None:
    with exclusive(path.with_suffix(".lock")), open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def delete_dictation(entry: dict, path: Path = HISTORY_PATH) -> bool:
    with exclusive(path.with_suffix(".lock")):
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for i in reversed(range(len(lines))):
            if lines[i] and json.loads(lines[i]) == entry:
                del lines[i]
                path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
                return True
    return False


def delete_call(folder: Path, recordings_dir: Path) -> None:
    """Removes a processed call; its audio goes too if wav2sum recorded it, not if it's the user's own file."""
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    audio = Path(meta["audio"]) if meta.get("audio") else None
    doomed = [folder]
    if audio and audio.exists():
        shutil.rmtree(folder.parent / ".cache" / file_hash(audio), ignore_errors=True)
        if audio.parent.resolve() == recordings_dir.resolve():
            doomed += [p for p in (audio, audio.with_suffix(".json")) if p.exists()]
    _remove(doomed)


def _remove(paths: list[Path]) -> None:
    if CAN_TRASH:
        subprocess.run(["trash", *map(str, paths)], check=True, capture_output=True)
        return
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
