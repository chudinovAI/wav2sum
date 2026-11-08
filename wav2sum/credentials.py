"""Cloud credentials: explicit environment override, Keychain, then opt-in private beta.

Keychain secrets cross Security.framework in memory, never process arguments.
https://developer.apple.com/documentation/security/secitemcopymatching(_:_:)
"""

import ctypes
import getpass
import json
import os
import sys
import warnings
from contextlib import ExitStack, contextmanager
from functools import cache
from pathlib import Path

SERVICE = "wav2sum.cloud"
PROVIDERS = {"DEEPGRAM_API_KEY": "Deepgram", "OPENAI_API_KEY": "OpenAI"}
_NOT_FOUND = -25300
_DUPLICATE = -25299
BETA_CREDENTIALS_FILE = "_beta_cloud_keys.json"
_BETA_PATH = Path(__file__).with_name(BETA_CREDENTIALS_FILE)


def has_bundled_credentials() -> bool:
    """Report only presence; passive setup never reads credential contents or Keychain."""
    return _BETA_PATH.is_file()


def _bundled_credentials() -> dict[str, str]:
    try:
        values = json.loads(_BETA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        raise RuntimeError("Bundled beta credentials are invalid; obtain a new private build") from None
    if (
        not isinstance(values, dict)
        or set(values) != set(PROVIDERS)
        or any(not isinstance(value, str) or not value.strip() for value in values.values())
    ):
        raise RuntimeError("Bundled beta credentials are invalid; obtain a new private build")
    return {name: value.strip() for name, value in values.items()}


def credential(name: str) -> str:
    if name not in PROVIDERS:
        raise ValueError("Unsupported cloud credential")
    if value := os.environ.get(name, "").strip():
        return value
    value = Keychain().get(name)
    if value and value.strip():
        return value.strip()
    if value := _bundled_credentials().get(name):
        return value
    raise RuntimeError(f"{name} is not configured; run 'wav2sum cloud-keys' or export {name}")


def configure() -> None:
    """Explicit one-time setup; gather both values before changing either item."""
    values = {}
    for name, provider in PROVIDERS.items():
        value = os.environ.get(name, "").strip()
        if not value:
            if not sys.stdin.isatty():
                raise RuntimeError("Run wav2sum cloud-keys in an interactive terminal for hidden input")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                try:
                    value = getpass.getpass(f"{provider} API key (hidden): ").strip()
                except getpass.GetPassWarning:
                    raise RuntimeError("Secure hidden input is unavailable; no keys were saved") from None
        if not value:
            raise ValueError(f"Empty {provider} API key; existing Keychain items were not changed")
        values[name] = value
    keychain = Keychain(allow_interaction=True)
    for name, value in values.items():
        keychain.set(name, value)
        print(f"{PROVIDERS[name]}: saved in macOS Keychain")
    print("Cloud keys are available to new terminals and the menu bar app. Restart an idle cloud daemon.")


@cache
def _native():
    if sys.platform != "darwin":
        raise RuntimeError("macOS Keychain is unavailable; export the cloud API keys on this platform")
    return _Native()


class _Native:
    def __init__(self):
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        pointer = ctypes.c_void_p
        pointers = ctypes.POINTER(pointer)
        self._bind(self.cf, "CFStringCreateWithCString", pointer, [pointer, ctypes.c_char_p, ctypes.c_uint32])
        self._bind(self.cf, "CFDataCreate", pointer, [pointer, pointer, ctypes.c_long])
        self._bind(
            self.cf, "CFDictionaryCreate", pointer, [pointer, pointers, pointers, ctypes.c_long, pointer, pointer]
        )
        self._bind(self.cf, "CFRelease", None, [pointer])
        self._bind(self.cf, "CFDataGetLength", ctypes.c_long, [pointer])
        self._bind(self.cf, "CFDataGetBytePtr", pointer, [pointer])
        self._bind(self.cf, "CFGetTypeID", ctypes.c_ulong, [pointer])
        self._bind(self.cf, "CFDataGetTypeID", ctypes.c_ulong, [])
        self._bind(self.security, "SecItemCopyMatching", ctypes.c_int32, [pointer, pointers])
        self._bind(self.security, "SecItemAdd", ctypes.c_int32, [pointer, pointers])
        self._bind(self.security, "SecItemUpdate", ctypes.c_int32, [pointer, pointer])

    @staticmethod
    def _bind(library, name, result, args):
        function = getattr(library, name)
        function.restype = result
        function.argtypes = args

    def constant(self, name):
        library = self.cf if name.startswith("kCF") else self.security
        return ctypes.c_void_p.in_dll(library, name).value

    def dictionary(self, values):
        count = len(values)
        keys = (ctypes.c_void_p * count)(*(self.constant(name) for name in values))
        refs = (ctypes.c_void_p * count)(*values.values())
        callbacks = [
            ctypes.addressof(ctypes.c_byte.in_dll(self.cf, name))
            for name in ("kCFTypeDictionaryKeyCallBacks", "kCFTypeDictionaryValueCallBacks")
        ]
        return self.cf.CFDictionaryCreate(None, keys, refs, count, *callbacks)


class Keychain:
    def __init__(self, *, allow_interaction: bool = False, service: str = SERVICE):
        self.native = _native()
        self.allow_interaction = allow_interaction
        self.service = service

    @contextmanager
    def _query(self, name, *, value=None, read=False):
        if name not in PROVIDERS:
            raise ValueError("Unsupported cloud credential")
        native = self.native
        with ExitStack() as refs:

            def owned(pointer):
                if not pointer:
                    raise RuntimeError("Could not allocate a Keychain request")
                refs.callback(native.cf.CFRelease, pointer)
                return pointer

            values = {
                "kSecClass": native.constant("kSecClassGenericPassword"),
                "kSecAttrService": owned(native.cf.CFStringCreateWithCString(None, self.service.encode(), 0x08000100)),
                "kSecAttrAccount": owned(native.cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)),
                "kSecAttrSynchronizable": native.constant("kCFBooleanFalse"),
            }
            if not self.allow_interaction:
                values["kSecUseAuthenticationUI"] = native.constant("kSecUseAuthenticationUIFail")
            if read:
                values["kSecReturnData"] = native.constant("kCFBooleanTrue")
                values["kSecMatchLimit"] = native.constant("kSecMatchLimitOne")
            if value is not None:
                encoded = value.encode("utf-8")
                values["kSecValueData"] = owned(native.cf.CFDataCreate(None, ctypes.c_char_p(encoded), len(encoded)))
            yield owned(native.dictionary(values))

    @staticmethod
    def _check(status):
        if status:
            raise RuntimeError(
                f"Keychain access unavailable (status {status}); unlock Keychain and run wav2sum cloud-keys"
            )

    def get(self, name: str) -> str | None:
        result = ctypes.c_void_p()
        with self._query(name, read=True) as query:
            status = self.native.security.SecItemCopyMatching(query, ctypes.byref(result))
        if status == _NOT_FOUND:
            return None
        self._check(status)
        if not result.value:
            raise RuntimeError("Keychain returned no credential data")
        try:
            cf = self.native.cf
            if cf.CFGetTypeID(result) != cf.CFDataGetTypeID():
                raise RuntimeError("Keychain returned invalid credential data")
            data = ctypes.string_at(cf.CFDataGetBytePtr(result), cf.CFDataGetLength(result))
            try:
                return data.decode("utf-8")
            except UnicodeError:
                raise RuntimeError("Keychain credential is not valid UTF-8") from None
        finally:
            self.native.cf.CFRelease(result)

    def set(self, name: str, value: str) -> None:
        if not value.strip():
            raise ValueError("An empty API key is not saved")
        with self._query(name, value=value) as query:
            status = self.native.security.SecItemAdd(query, None)
        if status == _DUPLICATE:
            encoded = value.encode("utf-8")
            data = self.native.cf.CFDataCreate(None, ctypes.c_char_p(encoded), len(encoded))
            if not data:
                raise RuntimeError("Could not allocate Keychain credential data")
            try:
                updates = self.native.dictionary({"kSecValueData": data})
                if not updates:
                    raise RuntimeError("Could not allocate Keychain update")
                try:
                    with self._query(name) as query:
                        status = self.native.security.SecItemUpdate(query, updates)
                finally:
                    self.native.cf.CFRelease(updates)
            finally:
                self.native.cf.CFRelease(data)
        self._check(status)
