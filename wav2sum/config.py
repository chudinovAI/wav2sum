import contextlib
import fcntl
import json
import logging
import os
import tomllib
from collections.abc import Iterator
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wav2sum" / "config.toml"
STATE_DIR = Path.home() / "Library" / "Application Support" / "wav2sum"
SOCKET_PATH = STATE_DIR / "daemon.sock"
LOG_PATH = STATE_DIR / "daemon.log"
HISTORY_PATH = STATE_DIR / "dictations.jsonl"
VOCABULARY_PATH = STATE_DIR / "vocabulary.json"
MAX_MESSAGE = 64 * 1024 * 1024


@dataclass
class DictationConfig:
    hotkey: str = "right_option"
    model: str = "gemma4:e4b-it-qat"
    cleanup: bool = True
    learn: bool = True
    dictionary: list[str] = field(default_factory=list)
    snippets: dict[str, str] = field(default_factory=dict)
    apps: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    me: str = "Я"
    them: str = "Собеседник"
    model: str = "gemma4:e4b-it-qat"
    num_ctx: int = 16384
    mic: str | None = None
    recordings_dir: Path = Path("~/wav2sum/recordings")
    output_dir: Path = Path("~/wav2sum/output")
    auto_record: bool = True
    call_apps: list[str] = field(default_factory=lambda: ["us.zoom", "com.microsoft.teams2", "Cisco-Systems.Spark"])
    dictation: DictationConfig = field(default_factory=DictationConfig)

    def __post_init__(self):
        self.recordings_dir = Path(self.recordings_dir).expanduser()
        self.output_dir = Path(self.output_dir).expanduser()


@contextlib.contextmanager
def exclusive(path: Path, wait: bool = True) -> Iterator[bool]:
    """Holds an flock on `path` across processes; yields False if `wait` is off and someone else holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    dictation = DictationConfig(**_known_keys(DictationConfig, raw.pop("dictation", {}), "dictation."))
    return Config(**_known_keys(Config, raw), dictation=dictation)


def _known_keys(cls, raw: dict, prefix: str = "") -> dict:
    known = {f.name for f in fields(cls)} - {"dictation"}
    for key in raw.keys() - known:
        logger.warning("%s: unknown setting %s%s", CONFIG_PATH, prefix, key)
    return {k: v for k, v in raw.items() if k in known}


def write_default_config(path: Path = CONFIG_PATH) -> None:
    if path.exists() and tomllib.loads(path.read_text(encoding="utf-8")):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    top = [f"{name} = {_toml(value)}" for name, value in _defaults(Config) if name != "dictation"]
    dictation = [f"{name} = {_toml(value)}" for name, value in _defaults(DictationConfig)]
    path.write_text("\n".join([*top, "", "[dictation]", *dictation]) + "\n", encoding="utf-8")


def _defaults(cls) -> list[tuple[str, object]]:
    values = []
    for f in fields(cls):
        value = f.default if f.default is not MISSING else f.default_factory()
        if value is not None:
            values.append((f.name, value))
    return values


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k, ensure_ascii=False)} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(str(value), ensure_ascii=False)
