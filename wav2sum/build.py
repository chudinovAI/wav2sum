import functools
import hashlib
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from wav2sum.config import exclusive

logger = logging.getLogger(__name__)

NATIVE_DIR = Path(__file__).parent / "native"
BUILD_DIR = Path.home() / "Library" / "Caches" / "wav2sum"
APP_PATH = Path.home() / "Applications" / "Wav2Sum.app"
APP_ID = "ai.chudinov.wav2sum"
SIGNING_IDENTITY = os.environ.get("WAV2SUM_SIGNING_IDENTITY", "wav2sum")


class BuildError(RuntimeError):
    pass


def capture_helper() -> Path:
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
    sources = sorted((NATIVE_DIR / "app").glob("*.swift"))
    icon_source = NATIVE_DIR / "icon" / "main.swift"
    serve = [sys.executable, "-m", "wav2sum", "serve"]
    digest = _digest([*sources, icon_source], extra=" ".join([*serve, _identity()]))
    info = {
        "CFBundleIdentifier": APP_ID,
        "CFBundleName": "Wav2Sum",
        "CFBundleExecutable": "Wav2Sum",
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "AppIcon",
        "CFBundleShortVersionString": digest,
        "LSMinimumSystemVersion": "14.4",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Wav2Sum listens to the microphone while you hold the dictation key.",
        "W2SServeCommand": serve,
    }
    plist_path = APP_PATH / "Contents" / "Info.plist"
    if plist_path.exists() and plistlib.loads(plist_path.read_bytes()).get("CFBundleShortVersionString") == digest:
        return APP_PATH

    logger.info("Building Wav2Sum.app …")
    tmp = BUILD_DIR / "Wav2Sum.app"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "Contents" / "MacOS").mkdir(parents=True)
    (tmp / "Contents" / "Resources").mkdir()
    (tmp / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    _swiftc(sources, tmp / "Contents" / "MacOS" / "Wav2Sum")
    _make_icon(NATIVE_DIR / "app" / "Logo.swift", icon_source, tmp / "Contents" / "Resources" / "AppIcon.icns")
    _sign(tmp, APP_ID)

    APP_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pkill", "-x", "Wav2Sum"], capture_output=True, check=False)
    shutil.rmtree(APP_PATH, ignore_errors=True)
    shutil.move(tmp, APP_PATH)
    return APP_PATH


def _make_icon(logo: Path, generator: Path, icns: Path) -> None:
    iconset = BUILD_DIR / "AppIcon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    _swiftc([logo, generator], BUILD_DIR / "make-icon")
    _run([str(BUILD_DIR / "make-icon"), str(iconset)])
    _run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)])


def _sign(path: Path, identifier: str) -> None:
    _run(["codesign", "-f", "-s", _identity(), "-i", identifier, str(path)])


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


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise BuildError(f"{Path(cmd[0]).name}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout
