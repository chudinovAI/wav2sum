import shutil

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
