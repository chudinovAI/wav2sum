"""Explicit app setup over local JSON stdin/stdout; no background consent or uploads."""

import json
import socket
import sys
from pathlib import Path

from wav2sum.config import (
    CLEANUP_MODEL,
    SOCKET_PATH,
    SettingsError,
    load_preferences,
    save_preferences,
    validate_settings,
)

MAX_SETUP_MESSAGE = 16 * 1024 * 1024


def _configure(values: dict) -> dict:
    values = validate_settings(values)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(15)
        try:
            connection.connect(str(SOCKET_PATH))
        except (FileNotFoundError, ConnectionRefusedError):
            return {"ok": True, "settings": save_preferences(values), "restart": False}
        # Once sent, never retry or write preferences independently after an ambiguous disconnect.
        connection.sendall((json.dumps({"id": 1, "cmd": "configure", "settings": values}) + "\n").encode())
        with connection.makefile("rb") as stream:
            line = stream.readline(MAX_SETUP_MESSAGE + 1)
        if not line or len(line) > MAX_SETUP_MESSAGE:
            raise SettingsError("Background process did not confirm settings; reopen settings to check")
        try:
            reply = json.loads(line)
        except ValueError:
            raise SettingsError("Background process returned an invalid settings reply") from None
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise SettingsError("Settings were refused: finish recording, dictation and queued jobs first")
        reply.pop("id", None)
        return reply


def _status() -> dict:
    from wav2sum.credentials import has_bundled_credentials
    from wav2sum.local_models import hardware_info, model_status, recommendation

    hardware = hardware_info()
    recommended = recommendation(hardware)
    values = load_preferences()
    if not values:
        values = {
            "completed": False,
            "mode": "local",
            "auto_process": False,
            "call_model": recommended["call_model"],
            "cleanup_model": CLEANUP_MODEL,
            "cloud_consent": False,
            "auto_cloud_consent": False,
            "allow_unsafe_nemotron": False,
        }
    return {
        "ok": True,
        "settings": values,
        "hardware": hardware,
        "recommendation": recommended,
        "models": model_status(values["call_model"], values["cleanup_model"]),
        "bundled_cloud_keys": has_bundled_credentials(),
    }


def _credentials(request: dict) -> dict:
    from wav2sum.credentials import Keychain

    values = {"DEEPGRAM_API_KEY": request.get("deepgram"), "OPENAI_API_KEY": request.get("openai")}
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise SettingsError("Enter both cloud keys before saving; existing keys were not changed")
    keychain = Keychain(allow_interaction=True)
    for name, value in values.items():
        keychain.set(name, value.strip())
    return {"ok": True}


def handle(request: dict) -> dict:
    if not isinstance(request, dict):
        raise SettingsError("Setup request must be an object")
    match request.get("action"):
        case "status":
            return _status()
        case "configure":
            return _configure(request.get("settings", request))
        case "credentials":
            return _credentials(request)
        case "prepare_models":
            from wav2sum.local_models import check_memory, prepare_models

            call_model = request.get("call_model")
            cleanup_model = request.get("cleanup_model", CLEANUP_MODEL)
            if call_model not in ("qwen", "nemotron") or cleanup_model != CLEANUP_MODEL:
                raise SettingsError("Choose a supported local model configuration")
            allow_unsafe = request.get("allow_unsafe_nemotron", False)
            if type(allow_unsafe) is not bool:
                raise SettingsError("The memory warning override must be a boolean")
            check_memory(call_model, allow_unsafe=allow_unsafe)
            token = request.get("hf_token")
            if token is not None and (not isinstance(token, str) or not token.strip()):
                raise SettingsError("Hugging Face token must be nonempty when provided")
            result = prepare_models(call_model, cleanup_model, hf_token=token)
            return {"ok": True, "models": result}
        case "examples":
            from wav2sum.sharing import list_examples

            return {"ok": True, "examples": list_examples()}
        case "export_examples":
            from wav2sum.sharing import export_examples

            path = request.get("path")
            if not isinstance(path, str) or not path:
                raise SettingsError("Choose a new export file")
            count = export_examples(Path(path), request.get("examples"), consent=request.get("consent") is True)
            return {"ok": True, "count": count, "path": path}
    raise SettingsError("Unknown setup action")


def main() -> None:
    try:
        source = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
        data = source.read(MAX_SETUP_MESSAGE + 1)
        if len(data) > MAX_SETUP_MESSAGE:
            raise SettingsError("Setup request is too large")
        reply = handle(json.loads(data))
    except SettingsError as error:
        reply = {"ok": False, "error": str(error)}
    except (ValueError, TypeError, KeyError):
        reply = {"ok": False, "error": "Invalid setup data or selected examples; review and retry"}
    except FileExistsError:
        reply = {"ok": False, "error": "Export file already exists; choose a new file"}
    except Exception:
        # Provider libraries may put credentials or transcript text in exception messages.
        reply = {"ok": False, "error": "Setup operation failed; check access, model prerequisites and connectivity"}
    print(json.dumps(reply, ensure_ascii=False))
