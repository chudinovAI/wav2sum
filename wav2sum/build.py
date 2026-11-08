import ctypes
import functools
import hashlib
import logging
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from wav2sum.config import exclusive

logger = logging.getLogger(__name__)

NATIVE_DIR = Path(__file__).parent / "native"
BUILD_DIR = Path.home() / "Library" / "Caches" / "wav2sum"
APP_PATH = Path.home() / "Applications" / "Wav2Sum.app"
APP_ID = "ai.chudinov.wav2sum"
SIGNING_IDENTITY = os.environ.get("WAV2SUM_SIGNING_IDENTITY", "wav2sum")
SOURCE_DIGEST = "Wav2SumSourceDigest"
_DEV_LAYOUT = {
    ".": {"Contents"},
    "Contents": {"Info.plist", "MacOS", "Resources", "_CodeSignature"},
    "Contents/MacOS": {"Wav2Sum"},
    "Contents/Resources": {"AppIcon.icns"},
    "Contents/_CodeSignature": {"CodeResources"},
}


class BuildError(RuntimeError):
    pass


def capture_helper() -> Path:
    bundled = os.environ.get("WAV2SUM_CAPTURE_HELPER")
    if bundled is not None:
        path = Path(bundled)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise BuildError("Bundled capture helper is missing or not executable.")
        return path
    with exclusive(BUILD_DIR / "build.lock"):
        return _capture_helper()


def _capture_helper() -> Path:
    source, plist = NATIVE_DIR / "capture.swift", NATIVE_DIR / "Info.plist"
    binary = BUILD_DIR / f"wav2sum-capture-{_digest([source, plist], extra=_identity())}"
    if binary.exists():
        return binary

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    for old in BUILD_DIR.glob("wav2sum-capture-*"):
        old.unlink()
    logger.info("Building wav2sum-capture …")
    tmp = binary.with_suffix(".tmp")
    _swiftc(
        [source],
        tmp,
        "-Xlinker",
        "-sectcreate",
        "-Xlinker",
        "__TEXT",
        "-Xlinker",
        "__info_plist",
        "-Xlinker",
        str(plist),
    )
    _sign(tmp, f"{APP_ID}.capture")
    tmp.rename(binary)
    return binary


def install_app() -> Path:
    return _install_app(bootstrap=False)


def bootstrap_watch_app() -> Path:
    """Explicitly build a watch-compatible app after verifying the existing dev bundle."""
    return _install_app(bootstrap=True)


def _install_app(*, bootstrap: bool) -> Path:
    serve = [sys.executable, "-m", "wav2sum", "serve"]
    identity = _named_identity() if bootstrap else _identity()
    snapshot = source_snapshot(NATIVE_DIR / "app")
    digest = source_digest(snapshot, serve, identity)
    try:
        if not APP_PATH.is_absolute():
            raise BuildError("App parent must be an absolute, non-symlink directory.")
        APP_PATH.parent.mkdir(parents=True, exist_ok=True)
        parent = _directory(APP_PATH.parent)
        try:
            old = APP_PATH.lstat()
        except FileNotFoundError:
            old = None
        if old and (not stat.S_ISDIR(old.st_mode) or old.st_dev != parent.st_dev):
            raise BuildError("Existing app must be a directory on the parent volume.")
        signature = verify_dev_app(APP_PATH, serve, require_digest=False) if bootstrap and old else None
        plist_path = APP_PATH / "Contents/Info.plist"
        original_plist = plist_path.read_bytes() if old and plist_path.is_file() else None
        info = plistlib.loads(original_plist) if original_plist is not None else {}
        marker = APP_PATH / "Contents/Resources" / SOURCE_DIGEST
        if not bootstrap and old and marker.is_file() and marker.read_bytes() == digest.encode("ascii"):
            return APP_PATH
        if info.get("W2SServeCommand") != serve:
            original_plist = None
        plist_bytes = (
            original_plist
            if original_plist is not None
            else plistlib.dumps(
                {
                    "CFBundleIdentifier": APP_ID,
                    "CFBundleName": "Wav2Sum",
                    "CFBundleExecutable": "Wav2Sum",
                    "CFBundlePackageType": "APPL",
                    "CFBundleIconFile": "AppIcon",
                    "CFBundleShortVersionString": digest,
                    "LSMinimumSystemVersion": "14.4",
                    "LSUIElement": True,
                    "NSMicrophoneUsageDescription": (
                        "Wav2Sum listens to the microphone while you hold the dictation key."
                    ),
                    "W2SServeCommand": serve,
                }
            )
        )
        _check_no_app_process()
        logger.info("Building Wav2Sum.app …")
        root = Path(tempfile.mkdtemp(prefix=".Wav2Sum-backup-", dir=APP_PATH.parent))
        staged = build_app(snapshot, root / "Wav2Sum.app", plist_bytes, identity, serve=serve)
        _check_target_unchanged(old)
        if (parent.st_dev, parent.st_ino) != ((current := _directory(APP_PATH.parent)).st_dev, current.st_ino):
            raise BuildError("App parent changed during build.")
        if bootstrap:
            staged_signature = verify_dev_app(staged, serve)
            if staged_signature[0] != identity or (signature and staged_signature != signature):
                raise BuildError("Staged signing requirement differs from the installed app.")
            if old and (
                verify_dev_app(APP_PATH, serve, require_digest=False) != signature
                or plist_path.read_bytes() != original_plist
            ):
                raise BuildError("App signature or Info.plist changed during build.")
        _check_no_app_process()
        _check_target_unchanged(old)
        _atomic_publish(staged, APP_PATH, old is not None)
        if old is not None:
            logger.info("Previous app preserved at %s", staged)
        else:
            root.rmdir()
        return APP_PATH
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise BuildError(f"Cannot safely build or publish app: {exc}") from exc


def _check_target_unchanged(old) -> None:
    try:
        current = APP_PATH.lstat()
    except FileNotFoundError:
        current = None
    if (old is None) != (current is None) or (
        old is not None and current is not None and (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise BuildError("App target changed during build.")


def _directory(path: Path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise BuildError(f"Not a directory: {path}")
    return info


def _file(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BuildError(f"Not a regular unlinked file: {path}")
    return info


def source_snapshot(source_dir: Path) -> dict[str, bytes]:
    """Capture app Swift files and the icon generator without following links."""
    source_dir = Path(source_dir)
    try:
        _directory(source_dir)
        _directory(source_dir.parent / "icon")
        paths = {path.name: path for path in sorted(source_dir.glob("*.swift"))}
        if not {"main.swift", "Logo.swift"} <= paths.keys():
            raise BuildError("Missing Swift app sources.")
        paths["icon/main.swift"] = source_dir.parent / "icon/main.swift"
        snapshot = {}
        for name, path in paths.items():
            _file(path)
            snapshot[name] = path.read_bytes()
        return snapshot
    except OSError as exc:
        raise BuildError(f"Cannot read source snapshot: {exc}") from exc


def source_digest(snapshot: dict[str, bytes], serve: list[str], identity: str) -> str:
    """Frame the command, signer and sorted relative source names/bytes."""
    digest = hashlib.sha256(b"wav2sum-source-v2\0")
    for group in (
        [arg.encode("utf-8") for arg in serve],
        [identity.encode("utf-8")],
        [part for name in sorted(snapshot) for part in (name.encode("utf-8"), snapshot[name])],
    ):
        digest.update(len(group).to_bytes(8, "big"))
        for value in group:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()[:12]


def build_app(
    snapshot: dict[str, bytes], destination: Path, plist_bytes: bytes, identity: str, *, serve: list[str]
) -> Path:
    """Build only a new stage; the caller owns its root, publication and lifetime.

    Live editor saves do not invalidate captured bytes. Only intermediates created
    by this call are removed; a failed stage is left for its caller to manage.
    """
    destination = Path(destination)
    snapshot = dict(snapshot)
    try:
        _directory(destination.parent)
        destination.mkdir()  # Never build over an installed app or an existing stage.
        macos, resources = destination / "Contents/MacOS", destination / "Contents/Resources"
        macos.mkdir(parents=True)
        resources.mkdir()
        plist = destination / "Contents/Info.plist"
        plist.write_bytes(plist_bytes)
        with tempfile.TemporaryDirectory(prefix=".app-sources-", dir=destination.parent) as directory:
            source_root = Path(directory)
            (source_root / "icon").mkdir()
            for name, data in snapshot.items():
                source = source_root / name
                source.write_bytes(data)
                source.chmod(0o400)
            copies = [source_root / name for name in sorted(snapshot) if name != "icon/main.swift"]
            _swiftc(copies, macos / "Wav2Sum")
            _make_icon(source_root / "Logo.swift", source_root / "icon/main.swift", resources / "AppIcon.icns")
            _file(macos / "Wav2Sum")
            _file(resources / "AppIcon.icns")
            if any((source_root / name).read_bytes() != data for name, data in snapshot.items()):
                raise BuildError("Staged source changed during compilation.")
            marker = resources / SOURCE_DIGEST
            digest = source_digest(snapshot, serve, identity).encode("ascii")
            marker.write_bytes(digest)
            _sign(destination, APP_ID, identity)
            if any((source_root / name).read_bytes() != data for name, data in snapshot.items()):
                raise BuildError("Staged source changed during signing.")
            if marker.read_bytes() != digest:
                raise BuildError("Staged source digest changed during signing.")
            if plist.read_bytes() != plist_bytes:
                raise BuildError("Staged plist changed during signing.")
        return destination
    except OSError as exc:
        raise BuildError(f"Cannot build app stage: {exc}") from exc


def verify_dev_app(path: Path, serve: list[str], *, require_digest: bool = True) -> tuple[str, str]:
    """Require the exact dev layout/environment and a unique matching named signer.

    Legacy bootstrap alone may omit the digest resource. The watch runtime also
    checks its native protocol marker. Callers must exclude concurrent writers;
    these checks are not protection against a hostile filesystem race.
    """
    path = Path(path)
    if len(serve) != 4 or not os.path.isabs(serve[0]) or list(serve[1:]) != ["-m", "wav2sum", "serve"]:
        raise BuildError("Invalid expected serve command.")
    try:
        _directory(path.parent)
        for relative, layout in _DEV_LAYOUT.items():
            expected = layout
            directory = path / relative
            _directory(directory)
            contents = {entry.name for entry in directory.iterdir()}
            if relative == "Contents/Resources":
                if SOURCE_DIGEST in contents:
                    expected = expected | {SOURCE_DIGEST}
                    marker = directory / SOURCE_DIGEST
                    _file(marker)
                    if not re.fullmatch(rb"[0-9a-f]{12}", marker.read_bytes()):
                        raise BuildError("Invalid source digest.")
                elif require_digest:
                    raise BuildError("Missing source digest; bootstrap the development app first.")
            if contents != expected:
                raise BuildError(f"Unexpected development bundle contents: {directory}")
        for relative in (
            "Contents/Info.plist",
            "Contents/MacOS/Wav2Sum",
            "Contents/Resources/AppIcon.icns",
            "Contents/_CodeSignature/CodeResources",
        ):
            _file(path / relative)
        info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
        if (
            not isinstance(info, dict)
            or info.get("CFBundleIdentifier") != APP_ID
            or info.get("CFBundleExecutable") != "Wav2Sum"
            or ("W2SBundledRuntime" in info and info["W2SBundledRuntime"] is not False)
            or info.get("W2SServeCommand") != list(serve)
        ):
            raise BuildError("Not the expected development bundle or Python environment.")
        signature = _signature(path)
        if signature[0] != _named_identity():
            raise BuildError("App signer does not match the configured identity.")
        return signature
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise BuildError(f"Invalid development bundle: {exc}") from exc


def _named_identity() -> str:
    if not SIGNING_IDENTITY or SIGNING_IDENTITY == "-":
        raise BuildError("A unique named signing identity is required for dev-watch.")
    found = _run(["security", "find-identity", "-p", "codesigning"])
    matches = re.findall(r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"\n]+)"(?:[ \t]+\([^()\n]*\))?[ \t]*$', found, re.M)
    selected = {sha.upper() for sha, name in matches if name == SIGNING_IDENTITY}
    if len(selected) != 1:
        raise BuildError("Signing identity is missing or ambiguous.")
    return next(iter(selected))


def _signature(path: Path) -> tuple[str, str]:
    _run(["codesign", "--verify", "--deep", "--strict", str(path)])
    with tempfile.TemporaryDirectory(prefix="wav2sum-cert-") as directory:
        _run(["codesign", "-d", "--extract-certificates=cert", str(path)], cwd=directory)
        cert = Path(directory) / "cert0"
        _file(cert)
        fingerprint = _run(
            ["openssl", "x509", "-inform", "DER", "-in", str(cert), "-noout", "-fingerprint", "-sha1"]
        ).strip()
    match = re.fullmatch(r"(?i)sha1 fingerprint=([0-9a-f]{2}(?::[0-9a-f]{2}){19})", fingerprint)
    if not match:
        raise BuildError("Invalid leaf fingerprint.")
    result = subprocess.run(["codesign", "-d", "-r-", str(path)], capture_output=True, text=True, check=False)
    requirements = [
        line.strip()
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if line.strip().startswith("designated =>")
    ]
    if result.returncode or len(requirements) != 1:
        raise BuildError("Missing or ambiguous designated requirement.")
    return match.group(1).replace(":", "").upper(), requirements[0]


def _check_no_app_process() -> None:
    try:
        result = subprocess.run(["pgrep", "-x", "Wav2Sum"], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise BuildError(f"Cannot enumerate Wav2Sum processes: {exc}") from exc
    if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
        raise BuildError("Wav2Sum is running or process enumeration failed; app was not changed.")


def _atomic_publish(source: Path, target: Path, swap: bool) -> None:
    """Publish exclusively or exchange same-volume directories, never delete either."""
    if sys.platform != "darwin":
        raise BuildError("Atomic app publication requires macOS renameatx_np.")
    try:
        _directory(source.parent)
        parent = _directory(target.parent)
        first = _directory(source)
        second = _directory(target) if swap else parent
        if first.st_dev != second.st_dev or (swap and first.st_ino == second.st_ino):
            raise BuildError("Publication requires distinct same-volume directories.")
        rename = ctypes.CDLL(None, use_errno=True).renameatx_np
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-2, os.fsencode(source), -2, os.fsencode(target), 0x2 if swap else 0x4) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    except (AttributeError, OSError) as exc:
        raise BuildError(f"Atomic app publication failed: {exc}") from exc


def _make_icon(logo: Path, generator: Path, icns: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=".icon-", dir=icns.parent) as scratch:
        iconset = Path(scratch) / "AppIcon.iconset"
        generator_binary = Path(scratch) / "make-icon"
        _swiftc([logo, generator], generator_binary)
        _run([str(generator_binary), str(iconset)])
        _run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)])


def _sign(path: Path, identifier: str, identity: str | None = None) -> None:
    _run(["codesign", "-f", "-s", _identity() if identity is None else identity, "-i", identifier, str(path)])


@functools.cache
def _identity() -> str:
    """A named certificate keeps macOS privacy permissions across rebuilds; ad-hoc ("-") loses them every time."""
    found = _run(["security", "find-identity", "-p", "codesigning"])
    for sha, name in re.findall(r'^\s*\d+\) ([0-9A-F]{40}) "([^"]+)"', found, re.M):
        if name == SIGNING_IDENTITY:
            return sha
    return "-"


def _swiftc(sources: list[Path], output: Path, *flags: str) -> None:
    if not shutil.which("swiftc"):
        raise BuildError("swiftc is required: install the Xcode Command Line Tools (`xcode-select --install`).")
    _run(["swiftc", "-O", *map(str, sources), "-o", str(output), *flags])


def _digest(files: list[Path], extra: str = "") -> str:
    h = hashlib.sha256(extra.encode())
    for f in files:
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _run(cmd: list[str], *, cwd: str | None = None) -> str:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise BuildError(f"{Path(cmd[0]).name}: {exc}") from exc
    if result.returncode != 0:
        raise BuildError(f"{Path(cmd[0]).name}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout
