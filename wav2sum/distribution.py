"""Build an internal, non-notarized macOS distribution without touching the installed app.

Run from the project's uv virtual environment with Xcode command-line tools.
The packaging build fetches a pinned FFmpeg source release; model weights and
user configuration are never bundled. Only explicit --private-beta embeds entered cloud keys.
"""

import argparse
import getpass
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import warnings
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from wav2sum import build
from wav2sum.credentials import BETA_CREDENTIALS_FILE, PROVIDERS

_OMIT_DIRS = {"__pycache__", "site-packages", "ensurepip", "test", "tests"}
_OMIT_FILES = {"direct_url.json", "uv_cache.json", "uv_build.json"}
_FFMPEG_VERSION = "8.1.3"
_FFMPEG_ARCHIVE = f"ffmpeg-{_FFMPEG_VERSION}.tar.xz"
_FFMPEG_URL = f"https://ffmpeg.org/releases/{_FFMPEG_ARCHIVE}"
_FFMPEG_SHA256 = "7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3"
_FFMPEG_CHECKSUM_REFERENCE = (
    "https://raw.githubusercontent.com/Homebrew/homebrew-core/"
    "f89dcb64714108b4f1427587ef5b1e7ec5194cf1/Formula/f/ffmpeg%408.rb"
)
_FFMPEG_LIBRARIES = (
    "libavcodec.62.dylib",
    "libavformat.62.dylib",
    "libavdevice.62.dylib",
    "libavfilter.11.dylib",
    "libavutil.60.dylib",
    "libswscale.9.dylib",
    "libswresample.6.dylib",
)
_FFMPEG_CONFIGURE = [
    "--prefix=/opt/wav2sum-ffmpeg",
    "--arch=aarch64",
    "--cc=clang",
    "--disable-static",
    "--enable-shared",
    "--disable-autodetect",
    "--disable-network",
    "--disable-ffplay",
    "--disable-ffprobe",
    "--disable-doc",
    "--disable-debug",
    "--install-name-dir=@rpath",
    "--extra-ldflags=-Wl,-rpath,@loader_path/../Frameworks -Wl,-rpath,@loader_path",
]
_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def _record_path(path: Path) -> bool:
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not any(part in {"__pycache__", ".cache", "tests", "test"} for part in path.parts)
        and path.name not in _OMIT_FILES
        and path.suffix not in {".pth", ".pyc", ".pyo"}
    )


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a runtime tree while rejecting links that escape its source root."""
    root = source.resolve(strict=True)
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if any(part in _OMIT_DIRS for part in relative.parts) or path.suffix in {".pyc", ".pyo", ".pth"}:
            continue
        target = destination / relative
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise build.BuildError(f"Unsafe runtime symlink: {relative}")
            link = os.readlink(path)
            if os.path.isabs(link):
                link = os.path.relpath(destination / resolved.relative_to(root), target.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        elif path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        else:
            raise build.BuildError(f"Unsupported runtime file: {relative}")


def _dependencies() -> list[metadata.Distribution]:
    """Resolve the installed project's runtime requirements including requested extras."""
    project = metadata.distribution("wav2sum")
    pending = [Requirement(item) for item in project.requires or []]
    extras_seen: dict[str, set[str]] = {}
    selected: dict[str, metadata.Distribution] = {}
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        try:
            dist = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            raise build.BuildError(f"Missing installed runtime dependency: {requirement.name}") from None
        if dist.version not in requirement.specifier:
            raise build.BuildError(
                f"Installed {requirement.name} {dist.version} does not satisfy {requirement.specifier}"
            )
        requested = set(requirement.extras)
        previous = extras_seen.get(name)
        if previous is not None and requested <= previous:
            continue
        extras_seen[name] = (previous or set()) | requested
        selected[name] = dist
        for text in dist.requires or []:
            child = Requirement(text)
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in {"", *extras_seen[name]}
            ):
                pending.append(child)
    return list(selected.values())


def _copy_dependencies(site: Path, target: Path) -> None:
    for dist in _dependencies():
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            try:
                details = json.loads(direct_url)
            except ValueError:
                raise build.BuildError(f"Invalid direct_url metadata for {dist.metadata['Name']}") from None
            if details.get("dir_info", {}).get("editable"):
                raise build.BuildError(f"Editable runtime dependency is not portable: {dist.metadata['Name']}")
        if dist.files is None:
            raise build.BuildError(f"Missing wheel RECORD for {dist.metadata['Name']}")
        for entry in dist.files:
            relative = Path(str(entry))
            if not _record_path(relative):
                continue
            source = Path(dist.locate_file(entry))
            # Only RECORD entries rooted in this environment's site-packages are allowed;
            # wheel scripts in bin/ and external paths are deliberately omitted.
            if not source.resolve().is_relative_to(site.resolve()):
                raise build.BuildError(f"Unsafe wheel file for {dist.metadata['Name']}: {relative}")
            if not source.is_file():
                raise build.BuildError(f"Missing wheel file for {dist.metadata['Name']}: {relative}")
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _copy_python(destination: Path, *, beta_keys: dict[str, str] | None = None) -> None:
    base = Path(sys.base_prefix).resolve()
    if sys.prefix == sys.base_prefix or not (base / "BUILD").is_file() or not base.name.startswith("cpython-"):
        raise build.BuildError("Run from a uv virtual environment backed by a standalone uv CPython installation.")
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    source_stdlib = base / "lib" / version
    source_python = base / "bin" / version
    if not source_python.is_file() or not source_stdlib.is_dir():
        raise build.BuildError("The standalone Python runtime is incomplete.")
    (destination / "bin").mkdir(parents=True)
    shutil.copy2(source_python, destination / "bin" / "python3")
    _copy_tree(source_stdlib, destination / "lib" / version)
    for library in (base / "lib").glob("*.dylib"):
        if library.is_symlink() and not library.resolve().is_relative_to(base):
            raise build.BuildError(f"Unsafe runtime symlink: {library.name}")
        shutil.copy2(library, destination / "lib" / library.name)
    site = Path(sysconfig.get_path("purelib"))
    target_site = destination / "lib" / version / "site-packages"
    target_site.mkdir(parents=True)
    _copy_dependencies(site, target_site)
    package = target_site / "wav2sum"
    package.mkdir()
    for source in Path(__file__).parent.glob("*.py"):
        if source.name in {"distribution.py", "evalset.py"}:
            continue
        if source.is_symlink():
            raise build.BuildError(f"Unsafe project source symlink: {source.name}")
        shutil.copy2(source, package / source.name)
    if beta_keys is not None:
        (package / BETA_CREDENTIALS_FILE).write_text(json.dumps(beta_keys), encoding="utf-8")
        (package / BETA_CREDENTIALS_FILE).chmod(0o644)


def _check_link_name(name: str) -> None:
    if name in {"@loader_path", "@executable_path", "@rpath"} or name.startswith(
        ("/usr/lib/", "/System/Library/", "@loader_path/", "@executable_path/", "@rpath/")
    ):
        return
    if name.startswith("/"):
        raise build.BuildError(f"Non-system absolute Mach-O reference: {name}")
    raise build.BuildError(f"Unsupported Mach-O reference: {name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_ffmpeg_source(archive: Path) -> None:
    if _sha256(archive) != _FFMPEG_SHA256:
        raise build.BuildError("FFmpeg source SHA-256 does not match the pinned release.")


def _extract_ffmpeg_source(archive: Path, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive) as stream:
            stream.extractall(directory, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise build.BuildError(f"Unsafe or invalid FFmpeg source archive: {exc}") from None


def _download_ffmpeg_source(archive: Path) -> None:
    # Use the system TLS/proxy stack for build-time source retrieval, not the
    # bundled interpreter's OpenSSL configuration. The pinned digest is required.
    with archive.open("xb"):
        pass
    result = subprocess.run(
        [
            "/usr/bin/curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            "120",
            "--max-filesize",
            str(32 * 1024 * 1024),
            "--output",
            str(archive),
            "--write-out",
            "%{url_effective}",
            _FFMPEG_URL,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or not result.stdout.startswith("https://ffmpeg.org/releases/"):
        raise build.BuildError("Could not download official FFmpeg source; check HTTPS/proxy access.")
    _verify_ffmpeg_source(archive)


def _source_build_command(command: list[str], cwd: Path, work: Path) -> str:
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(work),
        "TMPDIR": str(work),
        "LC_ALL": "C",
        "MACOSX_DEPLOYMENT_TARGET": "14.4",
    }
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, check=False)
    if result.returncode:
        raise build.BuildError(
            f"FFmpeg {Path(command[0]).name} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _build_ffmpeg(work: Path) -> tuple[Path, Path, Path]:
    build_root = work / "ffmpeg-build"
    build_root.mkdir()
    archive = build_root / _FFMPEG_ARCHIVE
    _download_ffmpeg_source(archive)
    _extract_ffmpeg_source(archive, build_root)
    source = build_root / f"ffmpeg-{_FFMPEG_VERSION}"
    if not (source / "configure").is_file():
        raise build.BuildError("Verified FFmpeg source archive is missing configure.")
    build_dir = build_root / "build"
    build_dir.mkdir()
    _source_build_command([str(source / "configure"), *_FFMPEG_CONFIGURE], build_dir, build_root)
    jobs = min(os.cpu_count() or 1, 8)
    _source_build_command(["/usr/bin/make", f"-j{jobs}"], build_dir, build_root)
    stage = build_root / "stage"
    _source_build_command(["/usr/bin/make", "install", f"DESTDIR={stage}"], build_dir, build_root)
    return stage / "opt" / "wav2sum-ffmpeg", source, archive


def _bundle_ffmpeg(work: Path, helpers: Path, licenses: Path) -> dict[Path, Path]:
    prefix, source, archive = _build_ffmpeg(work)
    frameworks = helpers.parent / "Frameworks"
    frameworks.mkdir()
    copies = {}
    for original, destination in [
        (prefix / "bin" / "ffmpeg", helpers / "ffmpeg"),
        *((prefix / "lib" / name, frameworks / name) for name in _FFMPEG_LIBRARIES),
    ]:
        if not original.is_file() or not original.resolve().is_relative_to(prefix.resolve()):
            raise build.BuildError(f"FFmpeg build is missing a safe runtime file: {original.name}")
        shutil.copy2(original, destination)
        build._sign(destination, f"{build.APP_ID}.ffmpeg")
        copies[original] = destination
    licenses.mkdir(parents=True)
    shutil.copy2(archive, licenses / _FFMPEG_ARCHIVE)
    for name in ("COPYING.LGPLv2.1", "LICENSE.md"):
        if not (source / name).is_file():
            raise build.BuildError(f"FFmpeg source is missing {name}.")
        shutil.copy2(source / name, licenses / name)
    manifest = {
        "notice": "Internal build; license and corresponding-source review is required before redistribution.",
        "source": {
            "url": _FFMPEG_URL,
            "sha256": _FFMPEG_SHA256,
            "checksum_reference": _FFMPEG_CHECKSUM_REFERENCE,
            "included_archive": _FFMPEG_ARCHIVE,
        },
        "configure": _FFMPEG_CONFIGURE,
        "environment": {"MACOSX_DEPLOYMENT_TARGET": "14.4", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "compiler": build._run(["/usr/bin/clang", "--version"]),
        "build_instructions": [
            "Extract the included source archive and configure from an empty build directory using the listed flags.",
            "Run make, then make install DESTDIR=<staging-directory>; copy the ffmpeg binary and shared libraries.",
            "The bundle signs copies using its existing local signing mechanism.",
        ],
        "ffmpeg_version_and_configure": build._run([str(helpers / "ffmpeg"), "-version"]),
        "binaries": [
            {"bundle_path": str(copy.relative_to(helpers.parent)), "sha256": _sha256(copy)} for copy in copies.values()
        ],
    }
    (licenses / "SOURCES.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return copies


def _link_names(path: Path) -> list[str]:
    output = build._run(["otool", "-L", str(path)])
    return [
        match.group(1)
        for line in output.splitlines()[1:]
        if (match := re.match(r"\s*(.+?)\s+\(compatibility version ", line))
    ]


def _dependency_names(path: Path) -> list[str]:
    identities = set(build._run(["otool", "-D", str(path)]).splitlines()[1:])
    return [name for name in _link_names(path) if name not in identities]


def _fixup_codec_plugins(runtime: Path, copies: dict[Path, Path]) -> None:
    """Ship only the torchcodec ABI matching our ffmpeg, with bundle-relative links."""
    libraries = {source.name: destination for source, destination in copies.items()}
    for codec in runtime.rglob("torchcodec"):
        cores = list(codec.glob("libtorchcodec_core*.dylib"))
        if not cores:
            continue
        compatible = []
        for core in cores:
            dependencies = [name for name in _link_names(core) if Path(name).name.startswith("libav")]
            if dependencies and all(Path(name).name in libraries for name in dependencies):
                compatible.append(core)
        if not compatible:
            raise build.BuildError("No torchcodec ABI matches the bundled ffmpeg libraries")
        selected = max(compatible, key=lambda path: int(re.search(r"core(\d+)", path.name)[1]))
        version = re.search(r"core(\d+)", selected.name)[1]
        for plugin in codec.glob("libtorchcodec_*"):
            if plugin.suffix not in {".dylib", ".so"}:
                continue
            if not re.search(rf"{version}\.(?:dylib|so)$", plugin.name):
                plugin.unlink()
                continue
            for name in _dependency_names(plugin):
                if target := libraries.get(Path(name).name):
                    relative = os.path.relpath(target, plugin.parent)
                    build._run(["install_name_tool", "-change", name, f"@loader_path/{relative}", str(plugin)])
            commands = build._run(["otool", "-l", str(plugin)])
            for rpath in re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.+?) \(offset \d+\)", commands):
                if rpath.startswith("/") and not rpath.startswith(("/usr/lib/", "/System/Library/")):
                    build._run(["install_name_tool", "-delete_rpath", rpath, str(plugin)])
            build._sign(plugin, f"{build.APP_ID}.codec")


def _remove_build_rpaths(runtime: Path) -> None:
    """Remove dead build-machine search paths from copied wheel binaries."""
    for path in runtime.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) not in _MACHO_MAGICS:
                continue
        commands = build._run(["otool", "-l", str(path)])
        rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.+?) \(offset \d+\)", commands)
        changed = False
        for rpath in rpaths:
            if not rpath.startswith("/") or rpath.startswith(("/usr/lib/", "/System/Library/")):
                continue
            for name in _dependency_names(path):
                if name.startswith("@rpath/") and (Path(rpath) / name.removeprefix("@rpath/")).is_file():
                    raise build.BuildError(f"Wheel still depends on a host-library search path: {path.name}")
            build._run(["install_name_tool", "-delete_rpath", rpath, str(path)])
            changed = True
        if changed:
            build._sign(path, f"{build.APP_ID}.runtime")


def _audit_macho(root: Path) -> str:
    minimum = "14.4"
    minimum_parts = (14, 4, 0)
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            magic = handle.read(4)
        if magic not in _MACHO_MAGICS:
            continue
        names = _dependency_names(path)
        try:
            for name in names:
                _check_link_name(name)
            commands = build._run(["otool", "-l", str(path)])
            for pattern in (
                r"cmd LC_BUILD_VERSION\b(?:(?!Load command).)*?\bminos\s+([\d.]+)",
                r"cmd LC_VERSION_MIN_MACOSX\b(?:(?!Load command).)*?\bversion\s+([\d.]+)",
            ):
                for version in re.findall(pattern, commands, re.DOTALL):
                    parts = tuple(int(part) for part in version.split("."))
                    padded = (*parts, 0, 0, 0)[:3]
                    if padded > minimum_parts:
                        minimum, minimum_parts = version, padded
            for rpath in re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.+?) \(offset \d+\)", commands):
                _check_link_name(rpath)
        except build.BuildError as error:
            raise build.BuildError(f"{path.relative_to(root)}: {error}") from None
    return minimum


def _isolated_env(home: Path, python: Path, helper: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "TMPDIR": str(home),
        "PYTHONHOME": str(python),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PATH": f"{helper.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        "WAV2SUM_CAPTURE_HELPER": str(helper),
    }


def _relocation_check(app: Path, home: Path) -> None:
    contents = app / "Contents"
    python = contents / "Resources" / "python"
    helper = contents / "Helpers" / "wav2sum-capture"
    script = """\
import os, pathlib, sys
root = pathlib.Path(sys.executable).resolve().parent.parent
assert all(not p or pathlib.Path(p).resolve().is_relative_to(root) for p in sys.path), sys.path
import wav2sum.cli, wav2sum.daemon, wav2sum.cloud, wav2sum.asr, wav2sum.models
import wav2sum.local_models, wav2sum.settings, wav2sum.sharing
import torch, torchaudio, transformers, pyannote.audio, mlx_lm
from wav2sum.build import capture_helper
assert capture_helper() == pathlib.Path(os.environ["WAV2SUM_CAPTURE_HELPER"])
import wave
audio = pathlib.Path.home() / "synthetic.wav"
with wave.open(str(audio), "wb") as stream:
    stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
    stream.writeframes(b"\\x00\\x00" * 16000)
from wav2sum.audio import load_audio
assert load_audio(audio, 1).shape == (16000, 1)
from torchcodec.decoders import AudioDecoder
assert AudioDecoder(str(audio)).get_all_samples().data.shape[-1] == 16000
"""
    result = subprocess.run(
        [str(python / "bin" / "python3"), "-I", "-B", "-c", script],
        cwd=home,
        env=_isolated_env(home, python, helper),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise build.BuildError(f"Relocated runtime self-check failed:\n{result.stderr.strip()}")
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise build.BuildError("Relocated capture helper is missing or not executable.")
    result = subprocess.run(
        [str(contents / "Helpers" / "ffmpeg"), "-version"],
        cwd=home,
        env=_isolated_env(home, python, helper),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise build.BuildError(f"Relocated ffmpeg self-check failed: {result.stderr.strip()}")


def _make_native(app: Path, work: Path) -> None:
    contents = app / "Contents"
    macos, resources, helpers = (contents / name for name in ("MacOS", "Resources", "Helpers"))
    for folder in (macos, resources, helpers):
        folder.mkdir(parents=True, exist_ok=True)
    sources = sorted((build.NATIVE_DIR / "app").glob("*.swift"))
    if not sources:
        raise build.BuildError("Native app sources are missing.")
    icon_source = build.NATIVE_DIR / "icon" / "main.swift"
    logo = build.NATIVE_DIR / "app" / "Logo.swift"
    info = {
        "CFBundleIdentifier": build.APP_ID,
        "CFBundleName": "Wav2Sum",
        "CFBundleExecutable": "Wav2Sum",
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "AppIcon",
        "CFBundleShortVersionString": build._digest([*sources, icon_source]),
        "LSMinimumSystemVersion": "15.0",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Wav2Sum listens to the microphone while you hold the dictation key.",
        "W2SBundledRuntime": True,
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    build._swiftc(sources, macos / "Wav2Sum", "-target", "arm64-apple-macosx15.0")
    capture = helpers / "wav2sum-capture"
    build._swiftc(
        [build.NATIVE_DIR / "capture.swift"],
        capture,
        "-target",
        "arm64-apple-macosx15.0",
        "-Xlinker",
        "-sectcreate",
        "-Xlinker",
        "__TEXT",
        "-Xlinker",
        "__info_plist",
        "-Xlinker",
        str(build.NATIVE_DIR / "Info.plist"),
    )
    icon_generator = work / "make-icon"
    iconset = work / "AppIcon.iconset"
    build._swiftc([logo, icon_source], icon_generator, "-target", "arm64-apple-macosx14.4")
    build._run([str(icon_generator), str(iconset)])
    build._run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")])
    build._sign(capture, f"{build.APP_ID}.capture")


def create_distribution(output: Path, *, no_dmg: bool = False, beta_keys: dict[str, str] | None = None) -> Path:
    if beta_keys is not None and (
        set(beta_keys) != set(PROVIDERS)
        or any(not isinstance(value, str) or not value.strip() for value in beta_keys.values())
    ):
        raise build.BuildError("Both beta cloud keys are required; no output was created")
    output = Path(output).absolute()
    if os.path.lexists(output):
        raise build.BuildError(f"Output already exists: {output}")
    if output.resolve().is_relative_to((Path.home() / "Applications").resolve()):
        raise build.BuildError("The output must not be inside ~/Applications.")
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise build.BuildError("Distribution requires macOS on Apple Silicon.")
    output.mkdir(mode=0o700 if beta_keys is not None else 0o777, parents=False, exist_ok=False)
    # Never reuse or delete an existing output. Failures leave this fresh directory for inspection.
    with tempfile.TemporaryDirectory(prefix=".build-", dir=output) as tmp:
        work = Path(tmp)
        staging = work / "image"
        app = staging / "Wav2Sum.app"
        _make_native(app, work)
        copies = _bundle_ffmpeg(work, app / "Contents" / "Helpers", app / "Contents" / "Resources" / "Licenses")
        _copy_python(app / "Contents" / "Resources" / "python", beta_keys=beta_keys)
        _fixup_codec_plugins(app / "Contents" / "Resources" / "python", copies)
        _remove_build_rpaths(app / "Contents" / "Resources" / "python")
        minimum_os = _audit_macho(app)
        info_path = app / "Contents" / "Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info["LSMinimumSystemVersion"] = minimum_os
        info_path.write_bytes(plistlib.dumps(info))
        build._sign(app, build.APP_ID)
        # Moving to a different parent ensures no launch path depends on the build location.
        moved = work / "relocated" / "Wav2Sum.app"
        moved.parent.mkdir()
        app.rename(moved)
        home = work / "isolated-home"
        home.mkdir()
        _relocation_check(moved, home)
        moved.rename(app)
        if not no_dmg:
            build._run(
                [
                    "hdiutil",
                    "create",
                    "-fs",
                    "HFS+",
                    "-volname",
                    "Wav2Sum Private Beta" if beta_keys is not None else "Wav2Sum Internal",
                    "-srcfolder",
                    str(staging),
                    "-format",
                    "UDZO",
                    str(output / ("Wav2Sum-Private-Beta.dmg" if beta_keys is not None else "Wav2Sum-Internal.dmg")),
                ]
            )
        app.rename(output / "Wav2Sum.app")
    return output / "Wav2Sum.app"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an internal, non-notarized standalone Wav2Sum app.")
    parser.add_argument("--output", type=Path, required=True, help="new output directory (must not already exist)")
    parser.add_argument("--no-dmg", action="store_true", help="build only the app bundle")
    parser.add_argument(
        "--private-beta", action="store_true", help="embed dedicated beta cloud keys from hidden terminal prompts"
    )
    args = parser.parse_args()
    beta_keys = None
    if args.private_beta:
        if not sys.stdin.isatty():
            parser.error("--private-beta requires an interactive terminal for hidden input; no keys were read")
        print("PRIVATE BETA: the app and DMG will contain extractable cloud keys. Share privately only.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                beta_keys = {
                    name: getpass.getpass(f"Dedicated beta {provider} API key (hidden): ").strip()
                    for name, provider in PROVIDERS.items()
                }
            except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
                parser.exit(1, "Secure key input failed or was cancelled; no output was created.\n")
    try:
        app = create_distribution(args.output, no_dmg=args.no_dmg, beta_keys=beta_keys)
    except (build.BuildError, OSError) as exc:
        parser.exit(1, f"Distribution failed: {exc}\n")
    print(f"Created {'PRIVATE BETA' if args.private_beta else 'internal'}, non-notarized app: {app}")
    if not args.no_dmg:
        name = "Wav2Sum-Private-Beta.dmg" if args.private_beta else "Wav2Sum-Internal.dmg"
        print(f"Created disk image: {app.parent / name}")


if __name__ == "__main__":
    main()
