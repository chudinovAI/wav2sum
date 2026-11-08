import contextlib
import fcntl
import json
import logging
import os
import stat
import tempfile
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
# How the user edited pasted dictations; the latest ones are the cleanup examples.
EDITS_PATH = STATE_DIR / "edits.jsonl"
VOCABULARY_PATH = STATE_DIR / "vocabulary.json"
MAX_MESSAGE = 64 * 1024 * 1024
AUTO = "auto"
PREFERENCES_PATH = STATE_DIR / "app-settings.json"
CLEANUP_MODEL = "mlx-community/gemma-4-E4B-it-qat-4bit"


@dataclass
class DictationConfig:
    hotkey: str = "right_option"
    model: str = "mlx-community/gemma-4-E4B-it-qat-4bit"
    cleanup: bool = True
    learn: bool = True
    dictionary: list[str] = field(default_factory=list)
    snippets: dict[str, str] = field(default_factory=dict)
    apps: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    me: str = "Я"
    them: str = "Собеседник"
    language: str = AUTO
    summary_model: str = "gpt-6.1-terra"
    num_ctx: int = 32768
    mic: str | None = None
    recordings_dir: Path = Path("~/wav2sum/recordings")
    output_dir: Path = Path("~/wav2sum/output")
    auto_record: bool = True
    auto_process: bool = False
    call_model: str = "nemotron"
    allow_unsafe_nemotron: bool = False
    call_apps: list[str] = field(default_factory=lambda: ["us.zoom", "com.microsoft.teams2", "Cisco-Systems.Spark"])
    dictation: DictationConfig = field(default_factory=DictationConfig)
    cloud: bool = field(default=False, init=False, repr=False)
    auto_cloud_consent: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        self.recordings_dir = Path(self.recordings_dir).expanduser()
        self.output_dir = Path(self.output_dir).expanduser()


@contextlib.contextmanager
def exclusive(path: Path, wait: bool = True) -> Iterator[bool]:
    """Holds an flock on `path` across processes; yields False if `wait` is off and someone else holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "w") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("Lock must be a regular file with a single link")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def load_config(path: Path = CONFIG_PATH, *, preferences_path: Path | None = None) -> Config:
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        dictation = DictationConfig(**_known_keys(DictationConfig, raw.pop("dictation", {}), "dictation."))
        cfg = Config(**_known_keys(Config, raw), dictation=dictation)
    else:
        cfg = Config()
    if preferences_path is not None or path == CONFIG_PATH:
        apply_preferences(cfg, load_preferences(preferences_path))
    return cfg


def _known_keys(cls, raw: dict, prefix: str = "") -> dict:
    known = {f.name for f in fields(cls) if f.init} - {"dictation"}
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
        if not f.init:
            continue
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


class SettingsError(ValueError):
    pass


def _validate_choices(values: dict) -> None:
    if values["call_model"] not in ("qwen", "nemotron"):
        raise SettingsError("Choose Qwen or Nemotron for local calls")
    if values["cleanup_model"] != CLEANUP_MODEL:
        raise SettingsError("Choose the supported local cleanup model")
    for key in ("auto_process", "cloud_consent", "auto_cloud_consent", "allow_unsafe_nemotron"):
        if type(values.get(key, False)) is not bool:
            raise SettingsError("Consent and processing choices must be booleans")


def validate_settings(values: dict) -> dict:
    if not isinstance(values, dict):
        raise SettingsError("Settings must be an object")
    required = {"completed", "mode", "auto_process", "call_model", "cleanup_model"}
    if not required <= values.keys():
        raise SettingsError("Complete all setup choices before saving")
    if values["completed"] is not True:
        raise SettingsError("Setup must be explicitly completed")
    if values["mode"] not in ("local", "cloud"):
        raise SettingsError("Choose local or cloud mode")
    _validate_choices(values)
    if values["mode"] == "cloud":
        if values.get("cloud_consent") is not True:
            raise SettingsError("Cloud requires explicit consent to provider transfers and charges")
        if values["auto_process"] and values.get("auto_cloud_consent") is not True:
            raise SettingsError("Automatic cloud call processing requires separate consent")
    result = {key: values[key] for key in required}
    for key in ("cloud_consent", "auto_cloud_consent"):
        result[key] = values.get(key, False)
    if "allow_unsafe_nemotron" in values:
        result["allow_unsafe_nemotron"] = values["allow_unsafe_nemotron"]
    return result


def load_preferences(path: Path | None = None) -> dict:
    path = PREFERENCES_PATH if path is None else Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SettingsError("App settings must be a regular file, not a link")
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise SettingsError("App settings are invalid; original file preserved") from None
    return validate_settings(values)


def save_preferences(values: dict, path: Path | None = None) -> dict:
    values = validate_settings(values)
    path = PREFERENCES_PATH if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(path.with_suffix(".lock")):
        if os.path.lexists(path):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SettingsError("App settings must be a regular file, not a link")
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".app-settings-", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(values, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)
    return values


def apply_preferences(cfg, preferences: dict) -> None:
    """App choices affect processing, never implicitly set a CLI's cloud flag."""
    if not preferences:
        return
    cfg.auto_process = preferences["auto_process"]
    cfg.call_model = preferences["call_model"]
    cfg.allow_unsafe_nemotron = preferences.get("allow_unsafe_nemotron", False)
    cfg.dictation.model = preferences["cleanup_model"]
    cfg.auto_cloud_consent = preferences["mode"] == "cloud" and preferences["auto_cloud_consent"]
