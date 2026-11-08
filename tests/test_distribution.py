import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from wav2sum import build, distribution


def test_output_must_be_new(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(build.BuildError, match="already exists"):
        distribution.create_distribution(output, no_dmg=True)


def test_bundled_capture_does_not_compile(tmp_path, monkeypatch):
    helper = tmp_path / "wav2sum-capture"
    helper.write_text("helper")
    helper.chmod(0o755)
    monkeypatch.setenv("WAV2SUM_CAPTURE_HELPER", str(helper))
    monkeypatch.setattr(build, "_capture_helper", lambda: pytest.fail("compiled on recipient"))
    assert build.capture_helper() == helper
    helper.chmod(0o644)
    with pytest.raises(build.BuildError, match="executable"):
        build.capture_helper()


def test_dependency_record_path_cannot_escape_site_packages(tmp_path):
    assert distribution._record_path(Path("package/module.py"))
    assert not distribution._record_path(Path("../../../bin/tool"))
    assert not distribution._record_path(Path("package/__pycache__/x.pyc"))
    assert not distribution._record_path(Path("package/tests/test_api.py"))
    assert not distribution._record_path(Path("editable.pth"))
    assert not distribution._record_path(Path("/absolute/path"))


def test_runtime_copy_refuses_external_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("private")
    (source / "link").symlink_to(outside)
    with pytest.raises(build.BuildError, match="symlink"):
        distribution._copy_tree(source, tmp_path / "target")


def test_relocation_environment_does_not_inherit_python_or_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/private/checkout")
    monkeypatch.setenv("HF_TOKEN", "secret")
    env = distribution._isolated_env(tmp_path, tmp_path / "python", tmp_path / "helper")
    assert env["HOME"] == str(tmp_path)
    assert env["PYTHONHOME"] == str(tmp_path / "python")
    assert env["WAV2SUM_CAPTURE_HELPER"] == str(tmp_path / "helper")
    assert "PYTHONPATH" not in env and "HF_TOKEN" not in env


def test_linkage_rejects_non_system_absolute_paths():
    distribution._check_link_name("/usr/lib/libSystem.B.dylib")
    distribution._check_link_name("@rpath/libfoo.dylib")
    with pytest.raises(build.BuildError, match="absolute"):
        distribution._check_link_name("/Users/someone/libfoo.dylib")


def test_macho_audit_reports_highest_deployment_target(tmp_path, monkeypatch):
    binary = tmp_path / "dependency.dylib"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(distribution, "_link_names", lambda _path: ["@rpath/dependency.dylib"])
    monkeypatch.setattr(build, "_run", lambda _command: "Load command 1\n cmd LC_BUILD_VERSION\n minos 26.2\n")
    assert distribution._audit_macho(tmp_path) == "26.2"


def test_shared_object_install_id_is_not_a_load_dependency(tmp_path, monkeypatch):
    binary = tmp_path / "_message.abi3.so"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    identity = "bazel-out/build/lib_message_binary.so"
    monkeypatch.setattr(distribution, "_link_names", lambda _path: [identity, "/usr/lib/libSystem.B.dylib"])
    monkeypatch.setattr(
        build,
        "_run",
        lambda command: f"{binary}:\n{identity}\n" if command[1] == "-D" else "",
    )
    assert distribution._audit_macho(tmp_path) == "14.4"


def test_output_cannot_be_a_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    output = tmp_path / "output"
    output.symlink_to(target, target_is_directory=True)
    with pytest.raises(build.BuildError, match="already exists"):
        distribution.create_distribution(output, no_dmg=True)


def test_ffmpeg_source_hash_is_required(tmp_path, monkeypatch):
    archive = tmp_path / "ffmpeg.tar.xz"
    archive.write_bytes(b"original source")
    monkeypatch.setattr(distribution, "_FFMPEG_SHA256", hashlib.sha256(b"original source").hexdigest())
    distribution._verify_ffmpeg_source(archive)
    archive.write_bytes(b"modified source")
    with pytest.raises(build.BuildError, match="SHA-256"):
        distribution._verify_ffmpeg_source(archive)


def test_ffmpeg_source_extraction_rejects_traversal(tmp_path):
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        stream.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(build.BuildError, match="Unsafe"):
        distribution._extract_ffmpeg_source(archive, tmp_path / "source")
    assert not (tmp_path / "escape").exists()


def test_installed_applications_tree_is_not_an_output(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    applications = tmp_path / "Applications"
    applications.mkdir()
    with pytest.raises(build.BuildError, match="Applications"):
        distribution.create_distribution(applications / "new-build")


def test_dependency_copy_only_uses_wheel_records(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    (site / "module.py").write_text("value = 1")
    (site / "unrecorded-secret").write_text("secret")
    (site / "editable.pth").write_text("/private/source")

    class Dist:
        files: ClassVar = [Path("module.py"), Path("editable.pth"), Path("../bin/tool")]
        metadata: ClassVar = {"Name": "example"}

        def read_text(self, name):
            return None

        def locate_file(self, entry):
            return site / entry

    monkeypatch.setattr(distribution, "_dependencies", lambda: [Dist()])
    target = tmp_path / "target"
    distribution._copy_dependencies(site, target)
    assert (target / "module.py").is_file()
    assert not (target / "editable.pth").exists()
    assert not (target / "unrecorded-secret").exists()


def test_editable_dependencies_are_refused(tmp_path, monkeypatch):
    class Editable:
        metadata: ClassVar = {"Name": "example"}

        def read_text(self, name):
            return '{"dir_info":{"editable":true}}'

    monkeypatch.setattr(distribution, "_dependencies", lambda: [Editable()])
    with pytest.raises(build.BuildError, match="Editable"):
        distribution._copy_dependencies(tmp_path, tmp_path / "output")


def test_packaged_codec_uses_matching_ffmpeg_not_build_machine_paths(tmp_path, monkeypatch):
    codec = tmp_path / "runtime" / "torchcodec"
    codec.mkdir(parents=True)
    for version in (6, 8):
        for kind in ("core", "custom_ops"):
            (codec / f"libtorchcodec_{kind}{version}.dylib").write_bytes(b"synthetic")
        (codec / f"libtorchcodec_pybind_ops{version}.so").write_bytes(b"synthetic")
    library = tmp_path / "Frameworks" / "1-libavcodec.62.dylib"
    library.parent.mkdir()
    library.write_bytes(b"synthetic")
    commands = []

    def names(path):
        if "core6" in path.name:
            return ["@rpath/libtorchcodec_core6.dylib", "@rpath/libavcodec.60.dylib"]
        if "core8" in path.name:
            return ["@rpath/libtorchcodec_core8.dylib", "@rpath/libavcodec.62.dylib"]
        return ["@rpath/" + path.name, "@rpath/libtorchcodec_core8.dylib"]

    monkeypatch.setattr(distribution, "_link_names", names)
    monkeypatch.setattr(build, "_sign", lambda *_args: None)
    monkeypatch.setattr(build, "_run", lambda command: commands.append(command) or "")
    distribution._fixup_codec_plugins(tmp_path / "runtime", {Path("/brew/libavcodec.62.dylib"): library})
    assert (codec / "libtorchcodec_core8.dylib").exists()
    assert not (codec / "libtorchcodec_core6.dylib").exists()
    assert not (codec / "libtorchcodec_custom_ops6.dylib").exists()
    assert not (codec / "libtorchcodec_pybind_ops6.so").exists()
    assert (codec / "libtorchcodec_pybind_ops8.so").exists()
    assert any(
        command[:3] == ["install_name_tool", "-change", "@rpath/libavcodec.62.dylib"]
        and command[3].startswith("@loader_path/")
        for command in commands
    )


@pytest.mark.parametrize("keys", [{}, {"DEEPGRAM_API_KEY": "one"}, {"DEEPGRAM_API_KEY": "dg", "OPENAI_API_KEY": ""}])
def test_invalid_beta_keys_do_not_create_output(tmp_path, keys):
    output = tmp_path / "private"
    with pytest.raises(build.BuildError, match="Both beta cloud keys"):
        distribution.create_distribution(output, beta_keys=keys)
    assert not output.exists()


def test_private_beta_requires_hidden_interactive_input(tmp_path, monkeypatch, capsys):
    output = tmp_path / "private"
    monkeypatch.setattr(distribution.sys, "argv", ["distribution", "--output", str(output), "--private-beta"])
    monkeypatch.setattr(distribution.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "must-not-copy-existing-dg")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-copy-existing-oa")
    with pytest.raises(SystemExit):
        distribution.main()
    assert not output.exists()
    captured = capsys.readouterr()
    assert "must-not-copy" not in captured.out + captured.err


def test_private_beta_refuses_getpass_echo_fallback(tmp_path, monkeypatch):
    import getpass
    import warnings

    output = tmp_path / "private"
    monkeypatch.setattr(distribution.sys, "argv", ["distribution", "--output", str(output), "--private-beta"])
    monkeypatch.setattr(distribution.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def insecure_input(_prompt):
        warnings.warn("echo unavailable", getpass.GetPassWarning, stacklevel=1)
        pytest.fail("must not read echoed keys")

    monkeypatch.setattr(getpass, "getpass", insecure_input)
    with pytest.raises(SystemExit):
        distribution.main()
    assert not output.exists()
