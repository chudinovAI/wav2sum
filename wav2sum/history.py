import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
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


def prune_dictations(path: Path = HISTORY_PATH, *, now: datetime | None = None) -> int:
    """Remove dictations older than seven days, retaining records of unknown age.

    Symlinks, hard links, and non-regular files are refused rather than replaced.
    Only writers using the history lock are protected from concurrent changes.
    """
    now = datetime.now(UTC) if now is None else now
    if now.utcoffset() is None:
        raise ValueError("Retention time must be timezone-aware")
    cutoff = now.astimezone(UTC) - timedelta(days=7)

    with exclusive(path.with_suffix(".lock")):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("History must be a regular file with a single link")

        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise OSError("History changed while opening")
            data = source.read()

        kept, removed = _retained_dictation_lines(data, cutoff)

        if not removed:
            return 0

        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                target = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with target as target:
                os.fchmod(target.fileno(), stat.S_IMODE(opened.st_mode))
                for line in kept:
                    target.write(line)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                os.unlink(temp_path)
        return removed


def _retained_dictation_lines(data: bytes, cutoff: datetime) -> tuple[list[bytes], int]:
    try:
        data.decode("utf-8")
        kept = []
        removed = 0
        parts = data.split(b"\n")
        for i, part in enumerate(parts):
            line = part + (b"\n" if i < len(parts) - 1 else b"")
            if not part.strip():
                kept.append(line)
                continue
            entry = json.loads(part, parse_constant=_invalid_json_constant)
            timestamp = entry.get("time") if isinstance(entry, dict) else None
            try:
                parsed = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
                expired = parsed is not None and parsed.utcoffset() is not None and parsed.astimezone(UTC) < cutoff
            except (ValueError, OverflowError):
                expired = False
            if expired:
                removed += 1
            else:
                kept.append(line)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid dictation history; no records were removed") from None
    return kept, removed


def _invalid_json_constant(_constant: str) -> None:
    raise ValueError("Invalid JSON constant")


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
