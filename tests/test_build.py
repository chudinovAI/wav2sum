import os
import plistlib
import shutil
import sys

import pytest

from wav2sum import build


@pytest.fixture
def builds(tmp_path, monkeypatch):
    native = tmp_path / "native"
    native.mkdir()
    for name in ("capture.swift", "Info.plist"):
        shutil.copy(build.NATIVE_DIR / name, native / name)
    compiled = []

    def swiftc(sources, output, *flags):
        compiled.append(output)
        output.write_text("binary")

    monkeypatch.setattr(build, "NATIVE_DIR", native)
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path / "cache")
    monkeypatch.setattr(build, "_swiftc", swiftc)
    monkeypatch.setattr(build, "_sign", lambda path, identifier: None)
    monkeypatch.setattr(build, "_identity", lambda: "-")
    return native, compiled


@pytest.fixture
def app_build(tmp_path, monkeypatch):
    native = tmp_path / "native"
    (native / "app").mkdir(parents=True)
    (native / "icon").mkdir()
    for source in (native / "app" / "Logo.swift", native / "app" / "main.swift", native / "icon" / "main.swift"):
        source.write_text("synthetic source")
    app = tmp_path / "Applications" / "Wav2Sum.app"
    app.parent.mkdir()
    monkeypatch.setattr(build, "NATIVE_DIR", native)
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path / "cache")
    monkeypatch.setattr(build, "APP_PATH", app)
    monkeypatch.setattr(build, "_identity", lambda: "synthetic")
    monkeypatch.setattr(build, "_swiftc", lambda sources, output, *flags: output.write_text("new"))

    def sign(path, identifier, identity):
        signature = path / "Contents/_CodeSignature"
        signature.mkdir()
        (signature / "CodeResources").write_text("synthetic signature")

    monkeypatch.setattr(build, "_sign", sign)
    monkeypatch.setattr(build, "_make_icon", lambda logo, generator, icns: icns.write_text("icon"))
    monkeypatch.setattr(build, "_check_no_app_process", lambda: None, raising=False)

    def publish(source, target, swap):
        if swap:
            intermediate = source.parent / "synthetic-intermediate"
            os.rename(target, intermediate)
            os.rename(source, target)
            os.rename(intermediate, source)
        else:
            assert not target.exists()
            os.rename(source, target)

    monkeypatch.setattr(build, "_atomic_publish", publish, raising=False)
    return app


def app_digest():
    return build.source_digest(
        build.source_snapshot(build.NATIVE_DIR / "app"),
        [sys.executable, "-m", "wav2sum", "serve"],
        build._identity(),
    )


def old_bundle(app):
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "old").write_text("original")


def test_unchanged_app_does_not_require_process_shutdown(app_build, monkeypatch):
    app = app_build
    old_bundle(app)
    digest = app_digest()
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": digest}))
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "Resources" / "Wav2SumSourceDigest").write_bytes(digest.encode("ascii"))
    monkeypatch.setattr(build, "_check_no_app_process", lambda: pytest.fail("unchanged app must not be replaced"))
    assert build.install_app() == app


def test_stale_signed_digest_forces_rebuild_even_with_matching_plist(app_build):
    app = app_build
    old_bundle(app)
    digest = app_digest()
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": digest}))
    resources = app / "Contents" / "Resources"
    resources.mkdir()
    (resources / "Wav2SumSourceDigest").write_bytes(b"bbbbbbbbbbbb")
    build.install_app()
    assert (app / "Contents" / "MacOS" / "Wav2Sum").read_text() == "new"
    assert (app / "Contents" / "Resources" / "Wav2SumSourceDigest").read_bytes() == digest.encode("ascii")


def test_initial_install_resource_is_signed_and_second_install_is_unchanged(app_build, monkeypatch):
    seen = []
    monkeypatch.setattr(
        build,
        "_sign",
        lambda path, identifier, identity: seen.append((path / "Contents/Resources/Wav2SumSourceDigest").read_bytes()),
    )
    build.install_app()
    assert len(seen) == 1 and len(seen[0]) == 12
    assert all(chr(c) in "0123456789abcdef" for c in seen[0])
    monkeypatch.setattr(build, "_check_no_app_process", lambda: pytest.fail("unchanged app was replaced"))
    assert build.install_app() == app_build


def test_install_publishes_and_preserves_old_bundle_as_sibling(app_build):
    app = app_build
    old_bundle(app)
    assert build.install_app() == app
    assert (app / "Contents" / "MacOS" / "Wav2Sum").read_text() == "new"
    backups = [p for p in app.parent.iterdir() if p != app]
    assert len(backups) == 1
    assert (backups[0] / "Wav2Sum.app/Contents/old").read_text() == "original"
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == build.APP_ID


@pytest.mark.parametrize("failed_stage", ["compile", "icon", "sign", "publish"])
def test_install_failure_preserves_old_bundle(app_build, monkeypatch, failed_stage):
    app = app_build
    old_bundle(app)

    def fail(*args, **kwargs):
        raise build.BuildError("synthetic failure")

    monkeypatch.setattr(
        build,
        {"compile": "_swiftc", "icon": "_make_icon", "sign": "_sign", "publish": "_atomic_publish"}[failed_stage],
        fail,
    )
    with pytest.raises(build.BuildError, match="synthetic failure"):
        build.install_app()
    assert (app / "Contents" / "old").read_text() == "original"


@pytest.mark.parametrize("check_number", [1, 2])
def test_process_check_failure_preserves_old_bundle(app_build, monkeypatch, check_number):
    app = app_build
    old_bundle(app)
    checks = []

    def check():
        checks.append(1)
        if len(checks) == check_number:
            raise build.BuildError("process running or enumeration unavailable")

    monkeypatch.setattr(build, "_check_no_app_process", check, raising=False)
    with pytest.raises(build.BuildError, match="process running or enumeration unavailable"):
        build.install_app()
    assert (app / "Contents" / "old").read_text() == "original"
    assert len(checks) == check_number


def test_install_into_absent_target_and_no_process_killing(app_build, monkeypatch):
    calls = []
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected subprocess"))
    monkeypatch.setattr(build, "_check_no_app_process", lambda: calls.append("check"))
    assert build.install_app() == app_build
    assert calls == ["check", "check"]
    assert (app_build / "Contents" / "MacOS" / "Wav2Sum").read_text() == "new"


@pytest.mark.parametrize("returncode,stdout,stderr", [(0, "123\\n", ""), (2, "", "error"), (1, "", "error")])
def test_process_enumeration_fails_closed(monkeypatch, returncode, stdout, stderr):
    from subprocess import CompletedProcess

    def run(cmd, **kwargs):
        assert cmd == ["pgrep", "-x", "Wav2Sum"]
        return CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(build.BuildError):
        build._check_no_app_process()


def test_process_enumeration_unavailable(monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError("no pgrep")

    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(build.BuildError):
        build._check_no_app_process()


@pytest.mark.parametrize("swap,flag", [(True, 2), (False, 4)])
def test_atomic_publish_uses_darwin_flags(tmp_path, monkeypatch, swap, flag):
    calls = []

    class Rename:
        def __call__(self, *args):
            calls.append(args)
            return 0

    class Library:
        renameatx_np = Rename()

    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(build.ctypes, "CDLL", lambda *args, **kwargs: Library())
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    if swap:
        target.mkdir()
    build._atomic_publish(source, target, swap)
    assert calls == [(-2, os.fsencode(source), -2, os.fsencode(target), flag)]


def test_icon_generation_leaves_fixed_cache_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path / "cache")
    fixed = build.BUILD_DIR / "AppIcon.iconset"
    fixed.mkdir(parents=True)
    (fixed / "important").write_text("keep")
    resources = tmp_path / "resources"
    resources.mkdir()
    calls = []
    monkeypatch.setattr(build, "_swiftc", lambda sources, output: output.write_text("binary"))
    monkeypatch.setattr(build, "_run", lambda cmd: calls.append(cmd) or "")
    build._make_icon(tmp_path / "logo", tmp_path / "generator", resources / "AppIcon.icns")
    assert (fixed / "important").read_text() == "keep"
    assert len(calls) == 2


def test_install_rejects_target_created_during_build(app_build, monkeypatch):
    app = app_build

    def sign(path, identifier, identity):
        old_bundle(app)

    monkeypatch.setattr(build, "_sign", sign)
    with pytest.raises(build.BuildError, match="target changed"):
        build.install_app()
    assert (app / "Contents" / "old").read_text() == "original"


def test_install_rejects_non_directory_target(app_build):
    app_build.write_text("original")
    with pytest.raises(build.BuildError):
        build.install_app()
    assert app_build.read_text() == "original"


def test_install_rejects_symlink_target(app_build):
    app = app_build
    other = app.parent / "other"
    old_bundle(other)
    app.symlink_to(other, target_is_directory=True)
    with pytest.raises(build.BuildError):
        build.install_app()
    assert app.is_symlink()
    assert (other / "Contents" / "old").read_text() == "original"


@pytest.fixture
def bootstrap(app_build, monkeypatch):
    identity = "A" * 40
    monkeypatch.setattr(build, "_identity", lambda: identity)
    monkeypatch.setattr(build, "SIGNING_IDENTITY", "synthetic name")
    monkeypatch.setattr(build, "_run", lambda cmd: f'  1) {identity} "synthetic name"\n')
    monkeypatch.setattr(build, "_signature", lambda path: (identity, "designated => synthetic"))
    app = app_build
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/_CodeSignature").mkdir()
    (app / "Contents/MacOS/Wav2Sum").write_text("original")
    (app / "Contents/Resources/AppIcon.icns").write_text("icon")
    (app / "Contents/_CodeSignature/CodeResources").write_text("signature")
    info = {
        "CFBundleIdentifier": build.APP_ID,
        "CFBundleExecutable": "Wav2Sum",
        "W2SServeCommand": [sys.executable, "-m", "wav2sum", "serve"],
    }
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    return app


@pytest.mark.parametrize(
    "identities", ["", "  1) " + "A" * 40 + ' "synthetic name"\n  2) ' + "B" * 40 + ' "synthetic name"\n']
)
def test_bootstrap_refuses_missing_or_ambiguous_identity(bootstrap, monkeypatch, identities):
    monkeypatch.setattr(build, "_run", lambda cmd: identities)
    with pytest.raises(build.BuildError):
        build.bootstrap_watch_app()
    assert (bootstrap / "Contents/MacOS/Wav2Sum").read_text() == "original"


@pytest.mark.parametrize("annotated", [False, True])
def test_bootstrap_accepts_macos_identity_sections_and_status(bootstrap, monkeypatch, annotated):
    identity, other = "A" * 40, "B" * 40
    suffix = " (CSSMERR_TP_NOT_TRUSTED)" if annotated else ""
    valid = "" if annotated else f'  1) {identity} "synthetic name"\n'
    listing = (
        "Policy: Code Signing\n  Matching identities\n"
        f'  1) {identity} "synthetic name"{suffix}\n'
        f'  2) {other} "Other developer"\n     2 identities found\n'
        "\n  Valid identities only\n"
        f'{valid}  2) {other} "Other developer"\n'
    )
    monkeypatch.setattr(build, "_run", lambda cmd: listing)
    assert build._named_identity() == identity
    assert build.bootstrap_watch_app() == bootstrap
    assert (bootstrap / "Contents/Resources/Wav2SumSourceDigest").is_file()


def test_bootstrap_refuses_adhoc_identity(bootstrap, monkeypatch):
    monkeypatch.setattr(build, "SIGNING_IDENTITY", "-")
    with pytest.raises(build.BuildError):
        build.bootstrap_watch_app()
    assert (bootstrap / "Contents/MacOS/Wav2Sum").read_text() == "original"


def test_bootstrap_refuses_standalone_and_signer_mismatch(bootstrap, monkeypatch):
    info_path = bootstrap / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["W2SBundledRuntime"] = True
    info_path.write_bytes(plistlib.dumps(info))
    with pytest.raises(build.BuildError):
        build.bootstrap_watch_app()
    del info["W2SBundledRuntime"]
    info_path.write_bytes(plistlib.dumps(info))
    monkeypatch.setattr(build, "_signature", lambda path: ("B" * 40, "requirement"))
    with pytest.raises(build.BuildError):
        build.bootstrap_watch_app()
    assert (bootstrap / "Contents/MacOS/Wav2Sum").read_text() == "original"


@pytest.mark.parametrize("has_digest", [False, True])
def test_bootstrap_forces_build_and_preserves_plist_bytes(bootstrap, has_digest):
    digest = app_digest()
    info_path = bootstrap / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleShortVersionString"] = digest
    info_path.write_bytes(plistlib.dumps(info))
    original_plist = info_path.read_bytes()
    if has_digest:
        (bootstrap / "Contents/Resources/Wav2SumSourceDigest").write_bytes(digest.encode("ascii"))
    assert build.bootstrap_watch_app() == bootstrap
    assert info_path.read_bytes() == original_plist
    assert (bootstrap / "Contents/MacOS/Wav2Sum").read_text() == "new"
    assert len((bootstrap / "Contents/Resources/Wav2SumSourceDigest").read_bytes()) == 12
    backups = [p for p in bootstrap.parent.iterdir() if p != bootstrap]
    assert len(backups) == 1
    assert (backups[0] / "Wav2Sum.app/Contents/MacOS/Wav2Sum").read_text() == "original"


@pytest.mark.parametrize("bootstrap_mode", [False, True])
def test_install_and_bootstrap_reuse_stage_builder_and_preserve_plist(bootstrap, monkeypatch, bootstrap_mode):
    plist = bootstrap / "Contents/Info.plist"
    original = plist.read_bytes() + b"\n\n"
    plist.write_bytes(original)
    calls = []
    stage_builder = build.build_app

    def stage(snapshot, destination, plist_bytes, identity, *, serve):
        assert destination != bootstrap and not destination.exists()
        assert plist_bytes == original and identity == "A" * 40
        calls.append(snapshot)
        return stage_builder(snapshot, destination, plist_bytes, identity, serve=serve)

    monkeypatch.setattr(build, "build_app", stage)
    install = build.bootstrap_watch_app if bootstrap_mode else build.install_app
    assert install() == bootstrap
    assert calls == [build.source_snapshot(build.NATIVE_DIR / "app")]
    assert plist.read_bytes() == original
    monkeypatch.setattr(build, "_check_no_app_process", lambda: pytest.fail("unchanged app must not be replaced"))
    assert build.install_app() == bootstrap  # The signed resource, not the preserved version field, is authoritative.


def test_bootstrap_does_not_consult_adhoc_default_identity(bootstrap, monkeypatch):
    monkeypatch.setattr(build, "_identity", lambda: pytest.fail("bootstrap must select a named identity directly"))
    assert build.bootstrap_watch_app() == bootstrap


def test_bootstrap_refuses_another_python_environment(bootstrap):
    info_path = bootstrap / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["W2SServeCommand"][0] = "/other/python"
    info_path.write_bytes(plistlib.dumps(info))
    original = info_path.read_bytes()
    with pytest.raises(build.BuildError, match="environment"):
        build.bootstrap_watch_app()
    assert info_path.read_bytes() == original
    assert (bootstrap / "Contents/MacOS/Wav2Sum").read_text() == "original"


def test_bootstrap_refuses_changed_designated_requirement(bootstrap, monkeypatch):
    def signature(path):
        requirement = "old requirement" if path == bootstrap else "new requirement"
        return ("A" * 40, requirement)

    monkeypatch.setattr(build, "_signature", signature)
    with pytest.raises(build.BuildError, match="requirement"):
        build.bootstrap_watch_app()
    assert (bootstrap / "Contents/MacOS/Wav2Sum").read_text() == "original"


def test_bootstrap_uses_captured_sources_despite_editor_save(bootstrap, monkeypatch):
    source = build.NATIVE_DIR / "app/Logo.swift"
    digest = app_digest()
    compiled = []

    def compile_changed(sources, output, *flags):
        compiled.extend(path.read_text() for path in sources if path.name == "Logo.swift")
        output.write_text("synthetic binary")
        source.write_text("changed during build")

    monkeypatch.setattr(build, "_swiftc", compile_changed)
    assert build.bootstrap_watch_app() == bootstrap
    assert compiled == ["synthetic source"]
    assert (bootstrap / "Contents/Resources/Wav2SumSourceDigest").read_text() == digest
    assert app_digest() != digest


def test_bootstrap_refuses_changed_signer_during_build(bootstrap, monkeypatch):
    signatures = iter([("A" * 40, "requirement"), ("A" * 40, "requirement"), ("B" * 40, "requirement")])
    monkeypatch.setattr(build, "_signature", lambda path: next(signatures))
    with pytest.raises(build.BuildError):
        build.bootstrap_watch_app()
    assert (bootstrap / "Contents/MacOS/Wav2Sum").read_text() == "original"


@pytest.mark.parametrize("stage", ["verify", "compile", "sign", "process", "publish"])
def test_bootstrap_failure_preserves_target(bootstrap, monkeypatch, stage):
    def fail(*args, **kwargs):
        if stage == "verify":
            raise build.BuildError("synthetic strict verification failure")
        raise build.BuildError("synthetic failure")

    if stage == "verify":
        monkeypatch.setattr(build, "_signature", fail)
    else:
        monkeypatch.setattr(
            build,
            {"compile": "_swiftc", "sign": "_sign", "process": "_check_no_app_process", "publish": "_atomic_publish"}[
                stage
            ],
            fail,
        )
    with pytest.raises(build.BuildError):
        build.bootstrap_watch_app()
    assert (bootstrap / "Contents/MacOS/Wav2Sum").read_text() == "original"


def test_bootstrap_absent_target_needs_unique_identity(app_build, monkeypatch):
    identity = "A" * 40
    monkeypatch.setattr(build, "_signature", lambda path: (identity, "requirement"))
    monkeypatch.setattr(build, "SIGNING_IDENTITY", "test")
    monkeypatch.setattr(build, "_identity", lambda: identity)
    monkeypatch.setattr(build, "_run", lambda cmd: "")
    with pytest.raises(build.BuildError):
        build.bootstrap_watch_app()
    assert not app_build.exists()
    monkeypatch.setattr(build, "_run", lambda cmd: f'  1) {identity} "test"\n')
    assert build.bootstrap_watch_app() == app_build


@pytest.mark.parametrize(
    "left,right",
    [
        ({"A.swift": b"ab", "B.swift": b"c"}, {"A.swift": b"a", "B.swift": b"bc"}),
        ({"A.swift": b"same"}, {"Renamed.swift": b"same"}),
        ({"A.swift": b"", "B.swift": b"same"}, {"B.swift": b"same"}),
        ({"app/main.swift": b"same"}, {"icon/main.swift": b"same"}),
        ({"a": b"b\x00c"}, {"a\x00b": b"c"}),
    ],
)
def test_source_digest_frames_names_and_file_boundaries(left, right):
    serve = ["/fake/python", "-m", "wav2sum", "serve"]
    assert build.source_digest(left, serve, "signer") != build.source_digest(right, serve, "signer")


def test_source_digest_frames_arguments_signer_and_order():
    snapshot = {"B.swift": b"b", "A.swift": b"a"}
    digest = build.source_digest(snapshot, ["a b", "c"], "d")
    assert digest == build.source_digest(dict(reversed(list(snapshot.items()))), ["a b", "c"], "d")
    assert digest != build.source_digest(snapshot, ["a", "b c"], "d")
    assert digest != build.source_digest(snapshot, ["a b", "c", "d"], "")
    assert len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)


def test_capture_helper_is_built_once_and_rebuilt_when_source_changes(builds):
    native, compiled = builds
    first = build.capture_helper()
    assert build.capture_helper() == first
    assert len(compiled) == 1

    (native / "capture.swift").write_text((native / "capture.swift").read_text() + "\n// changed\n")
    second = build.capture_helper()
    assert second != first
    assert len(compiled) == 2
    assert [p.name for p in second.parent.glob("wav2sum-capture-*")] == [second.name]


def test_named_certificate_is_preferred_over_ad_hoc_signing(monkeypatch):
    found = """
  1) 0123456789ABCDEF0123456789ABCDEF01234567 "Apple Development: someone"
  2) FEDCBA9876543210FEDCBA9876543210FEDCBA98 "wav2sum"
     2 valid identities found
"""
    monkeypatch.setattr(build, "_run", lambda cmd: found)
    assert build._identity.__wrapped__() == "FEDCBA9876543210FEDCBA9876543210FEDCBA98"
    monkeypatch.setattr(build, "SIGNING_IDENTITY", "missing")
    assert build._identity.__wrapped__() == "-"


def test_failed_tool_raises_build_error_with_its_output():
    with pytest.raises(build.BuildError, match="false"):
        build._run(["false"])
