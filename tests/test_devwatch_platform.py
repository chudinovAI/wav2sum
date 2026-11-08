"""Shared stage-only builder and development-bundle verification, with fake host tools."""

import os
import plistlib
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from wav2sum import build

IDENTITY = "A" * 40
COMMAND = ["/fake/python", "-m", "wav2sum", "serve"]
SOURCES = ("Daemon.swift", "DevWatch.swift", "Dictation.swift", "Logo.swift", "main.swift")


def make_bundle(path):
    (path / "Contents/MacOS").mkdir(parents=True)
    (path / "Contents/Resources").mkdir()
    (path / "Contents/_CodeSignature").mkdir()
    info = {
        "CFBundleIdentifier": build.APP_ID,
        "CFBundleExecutable": "Wav2Sum",
        "W2SServeCommand": COMMAND,
    }
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    (path / "Contents/MacOS/Wav2Sum").write_bytes(b"old binary")
    (path / "Contents/Resources/AppIcon.icns").write_bytes(b"icon")
    (path / "Contents/_CodeSignature/CodeResources").write_bytes(b"signature")
    return path


def sources(path):
    path.mkdir()
    for name in SOURCES:
        (path / name).write_text(name)
    icon = path.parent / "icon"
    icon.mkdir()
    (icon / "main.swift").write_text("icon generator")
    return path


class FakeTools:
    def __init__(self):
        self.calls = []
        self.compiled = []
        self.leaf = IDENTITY
        self.identities = f'  1) {IDENTITY} "wav2sum"\n'
        self.requirement = 'designated => identifier "ai.chudinov.wav2sum"'
        self.after = lambda args: None

    def __call__(self, args, *, cwd=None, **kwargs):
        self.calls.append((args, cwd))
        out, err = "", ""
        if args[0] == "security":
            out = self.identities
        elif args[0] == "openssl":
            out = f"sha1 Fingerprint={':'.join(self.leaf[i : i + 2] for i in range(0, 40, 2))}\n"
        elif args[0] == "codesign" and any(x.startswith("--extract-certificates") for x in args):
            Path(cwd, "cert0").write_bytes(b"fake public certificate")
        elif args[:3] == ["codesign", "-d", "-r-"]:
            err = self.requirement
        elif args[0] == "codesign" and "-s" in args:
            signature = Path(args[-1]) / "Contents/_CodeSignature"
            signature.mkdir()
            (signature / "CodeResources").write_bytes(b"signed")
        elif args[0] == "swiftc":
            self.compiled.append({Path(a): Path(a).read_bytes() for a in args if a.endswith(".swift")})
            Path(args[args.index("-o") + 1]).write_bytes(b"new binary")
        elif Path(args[0]).name == "make-icon":
            Path(args[1]).mkdir()
            (Path(args[1]) / "icon_16x16.png").write_bytes(b"fake png")
        elif args[0] == "iconutil":
            Path(args[args.index("-o") + 1]).write_bytes(b"new icon")
        self.after(args)
        return CompletedProcess(args, 0, out, err)


@pytest.fixture
def tools(monkeypatch):
    fake = FakeTools()
    monkeypatch.setattr(build.subprocess, "run", fake)
    monkeypatch.setattr(build.shutil, "which", lambda _: "/fake/swiftc")
    monkeypatch.setattr(build, "SIGNING_IDENTITY", "wav2sum")
    return fake


def stage_app(tmp_path, tools):
    app = make_bundle(tmp_path / "App.app")
    src = sources(tmp_path / "src")
    snapshot = build.source_snapshot(src)
    root = tmp_path / "owned"
    root.mkdir()
    stage = root / "Wav2Sum.app"
    build.build_app(snapshot, stage, (app / "Contents/Info.plist").read_bytes(), IDENTITY, serve=COMMAND)
    return app, src, snapshot, stage


def test_snapshot_digest_detects_edits_reverts_and_renames(tmp_path):
    src = sources(tmp_path / "src")
    icon = src.parent / "icon/main.swift"
    original = build.source_snapshot(src)
    assert original == {**{name: name.encode() for name in SOURCES}, "icon/main.swift": b"icon generator"}
    digest = build.source_digest(original, COMMAND, IDENTITY)
    (src / "Daemon.swift").write_text("source B")
    snapshot_b = build.source_snapshot(src)
    assert build.source_digest(snapshot_b, COMMAND, IDENTITY) != digest
    (src / "Daemon.swift").write_text("Daemon.swift")
    icon.write_text("icon B")
    assert build.source_digest(build.source_snapshot(src), COMMAND, IDENTITY) != digest
    icon.write_text("icon generator")
    assert build.source_digest(build.source_snapshot(src), COMMAND, IDENTITY) == digest
    assert build.source_digest(snapshot_b, COMMAND, IDENTITY) != digest
    (src / "Daemon.swift").rename(src / "Renamed.swift")
    assert build.source_digest(build.source_snapshot(src), COMMAND, IDENTITY) != digest
    assert build.source_digest(original, COMMAND, "B" * 40) != digest
    assert build.source_digest(original, ["/other/python", *COMMAND[1:]], IDENTITY) != digest


def test_added_source_is_compiled_from_captured_bytes(tmp_path, tools):
    src = sources(tmp_path / "src")
    (src / "Extra.swift").write_bytes(b"new Swift file")
    snapshot = build.source_snapshot(src)
    root = tmp_path / "owned"
    root.mkdir()
    build.build_app(snapshot, root / "Wav2Sum.app", b"plist", IDENTITY, serve=COMMAND)
    assert next(data for p, data in tools.compiled[0].items() if p.name == "Extra.swift") == b"new Swift file"


def test_verify_legacy_and_digest_resources(tmp_path, tools):
    app = make_bundle(tmp_path / "App.app")
    with pytest.raises(build.BuildError, match="digest"):
        build.verify_dev_app(app, COMMAND)
    assert build.verify_dev_app(app, COMMAND, require_digest=False) == (IDENTITY, tools.requirement)
    (app / "Contents/Resources/Wav2SumSourceDigest").write_bytes(b"012345abcdef")
    assert build.verify_dev_app(app, COMMAND) == (IDENTITY, tools.requirement)


@pytest.mark.parametrize(
    "value", [b"", b"012345abcde", b"012345abcdef\n", b"012345ABCDEF", b"012345abcdeg", b"\xff" * 12]
)
def test_invalid_digest_resource_rejected(tmp_path, tools, value):
    app = make_bundle(tmp_path / "App.app")
    (app / "Contents/Resources/Wav2SumSourceDigest").write_bytes(value)
    with pytest.raises(build.BuildError):
        build.verify_dev_app(app, COMMAND, require_digest=False)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
@pytest.mark.parametrize("relative", ["Contents/Resources/Wav2SumSourceDigest", "Contents/Resources/AppIcon.icns"])
def test_linked_or_non_file_resource_rejected(tmp_path, tools, kind, relative):
    app = make_bundle(tmp_path / "App.app")
    resource = app / relative
    if resource.exists():
        resource.unlink()
    target = tmp_path / "resource"
    target.write_bytes(b"012345abcdef")
    if kind == "symlink":
        resource.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, resource)
    else:
        resource.mkdir()
    with pytest.raises(build.BuildError):
        build.verify_dev_app(app, COMMAND, require_digest=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("CFBundleIdentifier", "other"),
        ("CFBundleExecutable", "other"),
        ("W2SBundledRuntime", True),
        ("W2SBundledRuntime", 0),
        ("W2SServeCommand", ["/fake/python", "-m", "wav2sum", "app"]),
    ],
)
def test_bad_info_rejected(tmp_path, tools, field, value):
    app = make_bundle(tmp_path / "App.app")
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info[field] = value
    info_path.write_bytes(plistlib.dumps(info))
    with pytest.raises(build.BuildError):
        build.verify_dev_app(app, COMMAND, require_digest=False)


def test_unexpected_bundle_content_rejected(tmp_path, tools):
    app = make_bundle(tmp_path / "App.app")
    (app / "Contents/Resources/key.json").write_bytes(b"not a key")
    with pytest.raises(build.BuildError):
        build.verify_dev_app(app, COMMAND, require_digest=False)


def test_common_stage_builds_captured_app_and_icon_then_digest_and_sign(tmp_path, tools):
    app, src, snapshot, stage = stage_app(tmp_path, tools)
    assert (stage / "Contents/MacOS/Wav2Sum").read_bytes() == b"new binary"
    assert (stage / "Contents/Resources/AppIcon.icns").read_bytes() == b"new icon"
    assert (stage / "Contents/Info.plist").read_bytes() == (app / "Contents/Info.plist").read_bytes()
    assert (stage / "Contents/Resources/Wav2SumSourceDigest").read_text() == build.source_digest(
        snapshot, COMMAND, IDENTITY
    )
    assert (app / "Contents/MacOS/Wav2Sum").read_bytes() == b"old binary"
    assert (app / "Contents/Resources/AppIcon.icns").read_bytes() == b"icon"
    assert build.verify_dev_app(stage, COMMAND) == (IDENTITY, tools.requirement)
    calls = [args for args, _ in tools.calls]
    compiles = [args for args in calls if args[0] == "swiftc"]
    assert len(compiles) == 2
    app_sources, icon_sources = tools.compiled
    assert {p.name: data for p, data in app_sources.items()} == {name: snapshot[name] for name in SOURCES}
    assert {p.name: data for p, data in icon_sources.items()} == {
        "Logo.swift": b"Logo.swift",
        "main.swift": b"icon generator",
    }
    assert next(p for p in app_sources if p.name == "Logo.swift") in icon_sources
    assert all(p.is_relative_to(stage.parent) and not p.is_relative_to(src) for p in app_sources | icon_sources)
    helper = Path(compiles[1][compiles[1].index("-o") + 1])
    helper_call = next(args for args in calls if args[0] == str(helper))
    iconset = Path(helper_call[1])
    iconutil = next(args for args in calls if args[0] == "iconutil")
    assert iconutil == ["iconutil", "-c", "icns", str(iconset), "-o", str(stage / "Contents/Resources/AppIcon.icns")]
    sign = next(args for args in calls if args[0] == "codesign" and "-s" in args)
    assert sign[sign.index("-s") + 1] == IDENTITY and sign[sign.index("-i") + 1] == build.APP_ID
    assert (
        calls.index(compiles[0])
        < calls.index(compiles[1])
        < calls.index(helper_call)
        < calls.index(iconutil)
        < calls.index(sign)
    )
    assert list(stage.parent.iterdir()) == [stage]  # Only this build's intermediates are removed.
    assert all(not p.exists() for p in app_sources | icon_sources)


@pytest.mark.parametrize("when", ["compile", "icon", "sign"])
def test_live_editor_saves_do_not_invalidate_captured_stage(tmp_path, tools, when):
    app = make_bundle(tmp_path / "App.app")
    src = sources(tmp_path / "src")
    snapshot = build.source_snapshot(src)
    root = tmp_path / "owned"
    root.mkdir()
    (root / "existing").write_text("keep")
    stage = root / "Wav2Sum.app"
    signed = []

    def after(args):
        if (
            (when == "compile" and args[0] == "swiftc")
            or (when == "icon" and args[0] == "iconutil")
            or (when == "sign" and args[0] == "codesign")
        ):
            (src / "Daemon.swift").write_text("new editor save")
            (src.parent / "icon/main.swift").write_text("new icon save")
            (src / "Extra.swift").write_text("extra")
        if args[0] == "codesign" and "-s" in args:
            signed.append((Path(args[-1]) / "Contents/Resources/Wav2SumSourceDigest").read_text())

    tools.after = after
    build.build_app(snapshot, stage, (app / "Contents/Info.plist").read_bytes(), IDENTITY, serve=COMMAND)
    assert next(data for p, data in tools.compiled[0].items() if p.name == "Daemon.swift") == b"Daemon.swift"
    assert next(data for p, data in tools.compiled[1].items() if p.name == "main.swift") == b"icon generator"
    assert signed == [build.source_digest(snapshot, COMMAND, IDENTITY)]
    assert build.source_snapshot(src) != snapshot
    assert (root / "existing").read_text() == "keep"
    assert {p.name for p in root.iterdir()} == {"existing", "Wav2Sum.app"}


@pytest.mark.parametrize("changed", ["source", "digest", "plist"])
def test_staged_tampering_fails_closed(tmp_path, tools, changed):
    def after(args):
        if changed == "source" and args[0] == "iconutil":
            staged_logo = next(p for p in tools.compiled[0] if p.name == "Logo.swift")
            staged_logo.chmod(0o600)
            staged_logo.write_bytes(b"changed staged source")
        if changed != "source" and args[0] == "codesign" and "-s" in args:
            relative = "Contents/Info.plist" if changed == "plist" else "Contents/Resources/Wav2SumSourceDigest"
            (Path(args[-1]) / relative).write_bytes(b"changed")

    tools.after = after
    with pytest.raises(build.BuildError, match=changed):
        stage_app(tmp_path, tools)
    assert (tmp_path / "App.app/Contents/MacOS/Wav2Sum").read_bytes() == b"old binary"
    assert {p.name for p in (tmp_path / "owned").iterdir()} == {"Wav2Sum.app"}


@pytest.mark.parametrize("failure", ["swiftc", "make-icon", "iconutil", "codesign", "missing-output"])
def test_stage_failure_preserves_original_and_cleans_own_intermediates(tmp_path, tools, monkeypatch, failure):
    def runner(args, **kwargs):
        if args[0] == failure or Path(args[0]).name == failure:
            return CompletedProcess(args, 1, "", "synthetic failure")
        if failure == "missing-output" and args[0] == "iconutil":
            return CompletedProcess(args, 0, "", "")
        return tools(args, **kwargs)

    monkeypatch.setattr(build.subprocess, "run", runner)
    with pytest.raises(build.BuildError):
        stage_app(tmp_path, tools)
    assert (tmp_path / "App.app/Contents/MacOS/Wav2Sum").read_bytes() == b"old binary"
    assert (tmp_path / "App.app/Contents/Resources/AppIcon.icns").read_bytes() == b"icon"
    assert {p.name for p in (tmp_path / "owned").iterdir()} == {"Wav2Sum.app"}


@pytest.mark.parametrize("relative", ["Daemon.swift", "Logo.swift", "main.swift", "icon/main.swift"])
@pytest.mark.parametrize("change", ["missing", "symlink", "hardlink"])
def test_missing_or_linked_sources_rejected(tmp_path, relative, change):
    src = sources(tmp_path / "src")
    source = src.parent / relative if relative.startswith("icon/") else src / relative
    source.unlink()
    if change == "symlink":
        source.symlink_to(src / "Dictation.swift")
    elif change == "hardlink":
        os.link(src / "Dictation.swift", source)
    if change == "missing" and relative == "Daemon.swift":
        assert "Daemon.swift" not in build.source_snapshot(src)  # Swift source set is intentionally dynamic.
    else:
        with pytest.raises(build.BuildError):
            build.source_snapshot(src)


@pytest.mark.parametrize(
    "identities", ["", f'  1) {IDENTITY} "wav2sum"\n  2) {IDENTITY} "wav2sum"\n', f'  1) {"B" * 40} "wav2sum"\n']
)
def test_identity_missing_ambiguous_or_mismatch(tmp_path, tools, identities):
    app = make_bundle(tmp_path / "App.app")
    tools.identities = identities
    with pytest.raises(build.BuildError):
        build.verify_dev_app(app, COMMAND, require_digest=False)
    assert not any(args[0] == "swiftc" for args, _ in tools.calls)


@pytest.mark.parametrize("name", ["", "-"])
def test_watch_requires_named_identity(tmp_path, tools, monkeypatch, name):
    monkeypatch.setattr(build, "SIGNING_IDENTITY", name)
    with pytest.raises(build.BuildError):
        build.verify_dev_app(make_bundle(tmp_path / "App.app"), COMMAND, require_digest=False)


def test_signature_failure_stops_before_compile(tmp_path, tools, monkeypatch):
    calls = []

    def failure(args, **kwargs):
        calls.append(args)
        return CompletedProcess(args, 1, "", "error")

    monkeypatch.setattr(build.subprocess, "run", failure)
    app = make_bundle(tmp_path / "App.app")
    with pytest.raises(build.BuildError):
        build.verify_dev_app(app, COMMAND, require_digest=False)
    assert calls == [["codesign", "--verify", "--deep", "--strict", str(app)]]


def test_build_requires_new_destination(tmp_path, tools):
    existing = make_bundle(tmp_path / "App.app")
    with pytest.raises(build.BuildError):
        build.build_app({}, existing, b"plist", IDENTITY, serve=COMMAND)
    assert (existing / "Contents/MacOS/Wav2Sum").read_bytes() == b"old binary"
    assert not tools.calls


@pytest.mark.parametrize("invalid", ["symlink", "same", "volume", "unsupported"])
def test_atomic_publish_rejects_unsafe_swap(tmp_path, monkeypatch, invalid):
    from types import SimpleNamespace

    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    if invalid == "symlink":
        link = tmp_path / "link"
        link.symlink_to(source)
        source = link
    elif invalid == "same":
        target = source
    elif invalid == "volume":
        actual = build._directory

        def directory(path):
            value = actual(path)
            return SimpleNamespace(st_dev=value.st_dev + 1, st_ino=value.st_ino) if path == target else value

        monkeypatch.setattr(build, "_directory", directory)
    else:
        monkeypatch.setattr(build.sys, "platform", "linux")
    monkeypatch.setattr(build.ctypes, "CDLL", lambda *a, **kw: pytest.fail("unsafe operation must not reach libc"))
    with pytest.raises(build.BuildError):
        build._atomic_publish(source, target, True)
    assert source.is_dir() and target.is_dir()


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS atomic swap required")
def test_real_atomic_publish_preserves_both_temporary_directories(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "value").write_text("old")
    (new / "value").write_text("new")
    build._atomic_publish(old, new, True)
    assert (old / "value").read_text() == "new"
    assert (new / "value").read_text() == "old"
