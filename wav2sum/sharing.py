"""Review and explicitly export local text examples without changing their sources."""

import hashlib
import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path

from wav2sum.config import EDITS_PATH, HISTORY_PATH, exclusive


def _invalid_constant(_value):
    raise ValueError("Invalid JSON constant")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _records(path):
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line, object_pairs_hook=_object, parse_constant=_invalid_constant)
                if not isinstance(row, dict):
                    raise ValueError("Invalid record")
                yield row
    except FileNotFoundError:
        return
    except (ValueError, UnicodeError):
        raise ValueError("Invalid example source") from None


def _example(row, source):
    fields = ("raw", "text", "language", "style")
    if any(not isinstance(row.get(key), str) or not row[key].strip() for key in fields):
        raise ValueError("Invalid example source")
    target = row.get("edited") if source == "edit" else row["text"]
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Invalid example source")
    try:
        # Include the complete record so any source change invalidates a prior review.
        encoded = json.dumps([source, row], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("Invalid example source") from None
    return {
        "id": hashlib.sha256(encoded).hexdigest(),
        "raw": row["raw"],
        "text": row["text"],
        "target": target,
        "language": row["language"],
        "style": row["style"],
        "source": source,
        "confirmed": source == "edit",
    }


def _read_examples(history_path, edits_path):
    examples = []
    seen = set()
    for path, source in ((history_path, "dictation"), (edits_path, "edit")):
        for row in _records(path):
            example = _example(row, source)
            if example["id"] not in seen:
                seen.add(example["id"])
                examples.append(example)
    return examples


def _locks(history_path, edits_path):
    stack = ExitStack()
    try:
        # Both writers use the same suffix lock convention. Sort to avoid deadlock.
        paths = {path.parent.resolve() / path.with_suffix(".lock").name for path in (history_path, edits_path)}
        for path in sorted(paths):
            stack.enter_context(exclusive(path))
    except BaseException:
        stack.close()
        raise
    return stack


def list_examples(history_path=HISTORY_PATH, edits_path=EDITS_PATH):
    """Return reviewable records; model output is not marked as human-confirmed."""
    history_path, edits_path = Path(history_path), Path(edits_path)
    with _locks(history_path, edits_path):
        return _read_examples(history_path, edits_path)


def export_examples(destination, selections, *, consent=False, history_path=HISTORY_PATH, edits_path=EDITS_PATH):
    """Publish selected reviewed examples to a new private JSONL file, never replacing one."""
    if consent is not True:
        raise ValueError("Training consent is required")
    if not isinstance(selections, list) or not selections:
        raise ValueError("Select at least one example")
    seen = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {"id", "target", "confirmed"}:
            raise ValueError("Invalid example selection")
        identifier, target = selection["id"], selection["target"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen
            or not isinstance(target, str)
            or not target.strip()
            or selection["confirmed"] is not True
        ):
            raise ValueError("Invalid example selection")
        seen.add(identifier)
    if not isinstance(destination, (str, os.PathLike)) or not os.fspath(destination):
        raise ValueError("A new destination is required")
    destination = Path(destination)
    history_path, edits_path = Path(history_path), Path(edits_path)
    with _locks(history_path, edits_path):
        current = {example["id"]: example for example in _read_examples(history_path, edits_path)}
        if not seen <= current.keys():
            raise ValueError("Examples changed or are no longer available; review them again")
        rows = [
            {
                "schema_version": 1,
                "raw": current[selection["id"]]["raw"],
                "target": selection["target"],
                "language": current[selection["id"]]["language"],
                "style": current[selection["id"]]["style"],
                "consent": "training",
            }
            for selection in selections
        ]
        try:
            data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("Invalid example selection") from None
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            try:
                target = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            # Hard-link publication is atomic and fails if any destination already exists.
            os.link(temp_path, destination)
        finally:
            if temp_path is not None:
                os.unlink(temp_path)
    return len(rows)
