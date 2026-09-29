"""Tests for toolchain installation inside the OpenShell sandbox.

The in-sandbox script runs here with the test's Python against crafted
archives, the way the sandbox runs it with the image's python3.
"""

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from agentic_ci.backends.openshell import provision, sandbox
from agentic_ci.backends.openshell.provision import INSTALL_SCRIPT, MARKER, OpenShellInstaller
from agentic_ci.toolchains import WRAPPER_DIR, InstallPlan, ToolchainError


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tar_bytes(members) -> bytes:
    """A tar.gz of (TarInfo fields, data) pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fields, data in members:
            info = tarfile.TarInfo(fields.pop("name"))
            for key, value in fields.items():
                setattr(info, key, value)
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return buf.getvalue()


def reg(name, data=b"x", mode=0o644):
    return {"name": name, "mode": mode}, data


def dir_(name):
    return {"name": name, "type": tarfile.DIRTYPE, "mode": 0o755}, None


def sym(name, target):
    return {"name": name, "type": tarfile.SYMTYPE, "linkname": target}, None


def hard(name, target):
    return {"name": name, "type": tarfile.LNKTYPE, "linkname": target}, None


def zip_bytes(entries) -> bytes:
    """A zip of (name, data, unix mode) triples."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            zf.writestr(info, data)
    return buf.getvalue()


class Sandbox:
    """A fake sandbox dir: ``root`` holds toolchains, ``cwd`` receives uploads."""

    def __init__(self, tmp_path: Path):
        self.cwd = tmp_path / "cwd"
        self.cwd.mkdir()
        self.root = tmp_path / "toolchains"

    def plan(self, data, archive="tar.gz", name="tool", version="1.2.3", **extra):
        return {
            "name": name,
            "version": version,
            "sha256": sha256(data),
            "archive": archive,
            "target": os.path.join(self.root, f"{name}-{version}"),
            "binary": "",
            "wrapper_dir": WRAPPER_DIR,
            "node_scripts": {},
            **extra,
        }

    def run(self, *args):
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", INSTALL_SCRIPT, *args],
            capture_output=True,
            text=True,
            cwd=self.cwd,
            timeout=60,
        )
        return result.returncode, result.stdout.strip()

    def install(self, data, plan=None, **kwargs):
        plan = plan or self.plan(data, **kwargs)
        name = sha256(data)
        (self.cwd / name).write_bytes(data)
        return self.run("install", json.dumps(plan), str(self.root), name)

    def check(self, plan):
        return self.run("check", json.dumps(plan), str(self.root))

    def leftovers(self):
        return sorted(os.listdir(self.root)) if self.root.exists() else []


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


class TestInstallTar:
    def test_installs_files_links_and_the_marker(self, box):
        data = tar_bytes(
            [
                dir_("./"),
                dir_("pkg/bin"),
                reg("pkg/bin/tool", b"#!/bin/sh\necho hi\n", 0o755),
                reg("pkg/lib/a.txt", b"a"),
                sym("pkg/bin/alias", "tool"),
                sym("pkg/lib/up", "../bin/tool"),
                hard("pkg/lib/b.txt", "pkg/lib/a.txt"),
            ]
        )
        assert box.install(data) == (0, "INSTALLED members=6")
        target = box.root / "tool-1.2.3"
        assert (target / "pkg/bin/tool").read_bytes() == b"#!/bin/sh\necho hi\n"
        assert os.stat(target / "pkg/bin/tool").st_mode & 0o111
        assert os.readlink(target / "pkg/bin/alias") == "tool"
        assert (target / "pkg/lib/b.txt").read_bytes() == b"a"
        assert json.loads((target / MARKER).read_text()) == {
            "name": "tool",
            "version": "1.2.3",
            "sha256": sha256(data),
        }
        assert box.leftovers() == ["tool-1.2.3"]
        assert os.listdir(box.cwd) == []  # the uploaded archive is removed

    @pytest.mark.parametrize(
        ("members", "reason"),
        [
            ([reg("../evil")], "member-path"),
            ([reg("pkg/../../evil")], "member-path"),
            ([reg("/etc/evil")], "member-path"),
            ([sym("pkg/link", "/etc/passwd")], "link"),
            ([sym("pkg/link", "../../outside")], "link"),
            ([sym("link", "..")], "link"),
            # Lexically inside, but "a" is the root, so "a/.." is its parent.
            ([sym("a", "."), sym("x", "a/..")], "link"),
            ([sym("a", "b"), sym("b", "a")], "link"),
            ([sym("pkg", "."), reg("pkg/file")], "member-path"),
            ([sym("d", "sub"), dir_("sub"), reg("d/../../evil")], "member-path"),
            ([hard("pkg/h", "/etc/passwd")], "member-path"),
            ([hard("pkg/h", "missing")], "link"),
            ([reg("a"), hard("pkg/h", "../a")], "member-path"),
            ([reg("a"), reg("a")], "duplicate"),
            ([reg("a"), reg("./a")], "duplicate"),
            (
                [({"name": "dev", "type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 3}, None)],
                "member-type",
            ),
            ([({"name": "fifo", "type": tarfile.FIFOTYPE}, None)], "member-type"),
            ([reg("")], "member-path"),
        ],
    )
    def test_escapes_are_refused_before_anything_is_extracted(self, box, members, reason):
        data = tar_bytes([reg("pkg/ok"), *members])
        assert box.install(data) == (3, f"REFUSED {reason}")
        assert box.leftovers() == []
        assert os.listdir(box.cwd) == []

    def test_too_large(self, box, monkeypatch):
        script = INSTALL_SCRIPT.replace("MAX_BYTES = 4 << 30", "MAX_BYTES = 10")
        monkeypatch.setattr(sys.modules[__name__], "INSTALL_SCRIPT", script)
        data = tar_bytes([reg("a", b"x" * 11)])
        assert box.install(data) == (3, "REFUSED too-large")

    def test_too_many_members(self, box, monkeypatch):
        script = INSTALL_SCRIPT.replace("MAX_MEMBERS = 200000", "MAX_MEMBERS = 2")
        monkeypatch.setattr(sys.modules[__name__], "INSTALL_SCRIPT", script)
        data = tar_bytes([reg("a"), reg("b"), reg("c")])
        assert box.install(data) == (3, "REFUSED too-many-members")

    def test_not_an_archive(self, box):
        assert box.install(b"not a tarball") == (3, "REFUSED format")

    def test_uploaded_archive_must_match_the_sha256(self, box):
        data = tar_bytes([reg("a")])
        plan = box.plan(data)
        plan["sha256"] = "0" * 64
        name = sha256(data)
        (box.cwd / name).write_bytes(data)
        assert box.run("install", json.dumps(plan), str(box.root), name) == (
            3,
            "REFUSED checksum",
        )
        assert box.leftovers() == []

    def test_archive_name_must_be_a_sha256(self, box):
        data = tar_bytes([reg("a")])
        (box.cwd / "evil.tgz").write_bytes(data)
        code, out = box.run("install", json.dumps(box.plan(data)), str(box.root), "evil.tgz")
        assert (code, out) == (3, "REFUSED archive-name")

    def test_archive_name_must_not_be_a_path(self, box):
        data = tar_bytes([reg("a")])
        (box.cwd / "sub").mkdir()
        (box.cwd / "sub" / sha256(data)).write_bytes(data)
        code, out = box.run(
            "install", json.dumps(box.plan(data)), str(box.root), "sub/" + sha256(data)
        )
        assert (code, out) == (3, "REFUSED archive-name")

    def test_a_symlinked_upload_is_refused(self, box, tmp_path):
        data = tar_bytes([reg("a")])
        real = tmp_path / "planted"
        real.write_bytes(data)
        (box.cwd / sha256(data)).symlink_to(real)
        code, out = box.run("install", json.dumps(box.plan(data)), str(box.root), sha256(data))
        assert (code, out) == (3, "REFUSED upload")
        assert box.leftovers() == []

    def test_an_upload_that_is_not_a_regular_file_is_refused(self, box):
        data = tar_bytes([reg("a")])
        os.mkfifo(box.cwd / sha256(data))
        code, out = box.run("install", json.dumps(box.plan(data)), str(box.root), sha256(data))
        assert (code, out) == (3, "REFUSED upload")

    def test_extraction_reads_the_verified_private_copy(self, box, monkeypatch):
        # Swapping the upload after the hash cannot change what is extracted:
        # the script hashes and extracts one private copy.
        good = tar_bytes([reg("good")])
        name = sha256(good)
        (box.cwd / name).write_bytes(good)
        script = INSTALL_SCRIPT.replace(
            "        return extract(plan, archive)",
            "        open(" + repr(str(box.cwd / name)) + ", 'wb').write(b'swapped')\n"
            "        return extract(plan, archive)",
        )
        assert script != INSTALL_SCRIPT
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, "install"]
            + [json.dumps(box.plan(good)), str(box.root), name],
            capture_output=True,
            text=True,
            cwd=box.cwd,
            timeout=60,
        )
        assert result.stdout.strip() == "INSTALLED members=1"
        assert (box.root / "tool-1.2.3" / "good").exists()
        assert box.leftovers() == ["tool-1.2.3"]

    @pytest.mark.parametrize(
        "change",
        [
            {"target": "/tmp/elsewhere"},
            {"name": "../x"},
            {"version": "1.2.3/../../x"},
            {"sha256": "nothex"},
        ],
    )
    def test_target_must_be_the_expected_dir(self, box, change):
        data = tar_bytes([reg("a")])
        plan = {**box.plan(data), **change}
        assert box.install(data, plan=plan) == (3, "REFUSED target")

    def test_reinstall_replaces_a_stale_dir(self, box):
        stale = box.root / "tool-1.2.3"
        stale.mkdir(parents=True)
        (stale / "junk").write_text("old")
        data = tar_bytes([reg("new")])
        assert box.install(data)[0] == 0
        assert sorted(os.listdir(stale)) == [MARKER, "new"]

    def test_failed_extraction_leaves_the_old_install(self, box):
        good = tar_bytes([reg("good")])
        box.install(good)
        bad = tar_bytes([reg("../evil")])
        plan = box.plan(bad)
        assert box.install(bad, plan=plan)[0] == 3
        assert (box.root / "tool-1.2.3" / "good").exists()
        assert box.leftovers() == ["tool-1.2.3"]


class TestInstallZipAndBinary:
    def test_zip_keeps_exec_bits(self, box):
        data = zip_bytes(
            [
                ("bin/", b"", stat.S_IFDIR | 0o755),
                ("bin/protoc", b"ELF", stat.S_IFREG | 0o755),
                ("include/a.proto", b"syntax", stat.S_IFREG | 0o644),
            ]
        )
        assert box.install(data, archive="zip") == (0, "INSTALLED members=2")
        target = box.root / "tool-1.2.3"
        assert os.stat(target / "bin/protoc").st_mode & 0o777 == 0o755
        assert os.stat(target / "include/a.proto").st_mode & 0o777 == 0o644

    @pytest.mark.parametrize(
        ("entries", "reason"),
        [
            ([("../evil", b"x", stat.S_IFREG | 0o644)], "member-path"),
            ([("/abs", b"x", stat.S_IFREG | 0o644)], "member-path"),
            ([("link", b"/etc/passwd", stat.S_IFLNK | 0o777)], "member-type"),
            ([("a", b"x", stat.S_IFREG | 0o644), ("./a", b"y", stat.S_IFREG | 0o644)], "duplicate"),
        ],
    )
    def test_zip_escapes_are_refused(self, box, entries, reason):
        data = zip_bytes(entries)
        assert box.install(data, archive="zip") == (3, f"REFUSED {reason}")
        assert box.leftovers() == []

    def test_binary(self, box):
        data = b"\x7fELF shfmt"
        assert box.install(data, archive="binary", binary="shfmt") == (0, "INSTALLED members=1")
        path = box.root / "tool-1.2.3" / WRAPPER_DIR / "shfmt"
        assert path.read_bytes() == data
        assert os.stat(path).st_mode & 0o777 == 0o755

    @pytest.mark.parametrize("binary", ["", "../x", "a/b", ".hidden"])
    def test_binary_name_is_checked(self, box, binary):
        assert box.install(b"x", archive="binary", binary=binary) == (3, "REFUSED name")

    def test_node_launchers(self, box):
        data = tar_bytes([reg("package/bin/pnpm.cjs", b"// js")])
        scripts = {"pnpm": "package/bin/pnpm.cjs"}
        assert box.install(data, node_scripts=scripts)[0] == 0
        launcher = box.root / "tool-1.2.3" / WRAPPER_DIR / "pnpm"
        target = box.root / "tool-1.2.3" / "package/bin/pnpm.cjs"
        assert launcher.read_text() == f'#!/bin/sh\nexec node {target} "$@"\n'
        assert os.stat(launcher).st_mode & 0o111

    @pytest.mark.parametrize(
        "scripts", [{"../pnpm": "package/x"}, {"pnpm": "../../x"}, {"pnpm": ""}]
    )
    def test_node_launcher_names_are_checked(self, box, scripts):
        data = tar_bytes([reg("package/x")])
        code, out = box.install(data, node_scripts=scripts)
        assert code == 3 and out in ("REFUSED name", "REFUSED member-path")
        assert box.leftovers() == []

    def test_unknown_archive_kind(self, box):
        assert box.install(b"x", archive="rar") == (3, "REFUSED format")


class TestCheck:
    def test_absent_then_present(self, box):
        data = tar_bytes([reg("a")])
        plan = box.plan(data)
        assert box.check(plan) == (0, "ABSENT")
        box.install(data)
        assert box.check(plan) == (0, "PRESENT")

    def test_other_sha256_is_absent(self, box):
        data = tar_bytes([reg("a")])
        box.install(data)
        assert box.check({**box.plan(data), "sha256": "1" * 64}) == (0, "ABSENT")

    def test_symlinked_marker_is_absent(self, box, tmp_path):
        data = tar_bytes([reg("a")])
        plan = box.plan(data)
        target = Path(plan["target"])
        target.mkdir(parents=True)
        fake = tmp_path / "fake-marker"
        fake.write_text(json.dumps({"name": "tool", "version": "1.2.3", "sha256": sha256(data)}))
        (target / MARKER).symlink_to(fake)
        assert box.check(plan) == (0, "ABSENT")

    def test_garbage_marker_is_absent(self, box):
        data = tar_bytes([reg("a")])
        plan = box.plan(data)
        Path(plan["target"]).mkdir(parents=True)
        (Path(plan["target"]) / MARKER).write_text("{nope")
        assert box.check(plan) == (0, "ABSENT")

    def test_unexpected_errors_print_only_the_class(self, box):
        assert box.run("check", "{not json", str(box.root)) == (4, "FAILED JSONDecodeError")


class TestDiscard:
    def test_removes_the_upload(self, box):
        (box.cwd / ("a" * 64)).write_bytes(b"partial")
        assert box.run("discard", "a" * 64) == (0, "DISCARDED")
        assert os.listdir(box.cwd) == []

    def test_missing_upload_is_fine(self, box):
        assert box.run("discard", "a" * 64) == (0, "DISCARDED")

    @pytest.mark.parametrize("name", ["../x", "evil", "a" * 63])
    def test_only_sha256_names(self, box, name):
        (box.cwd / "evil").write_bytes(b"keep")
        assert box.run("discard", name) == (3, "REFUSED archive-name")
        assert (box.cwd / "evil").exists()


PLAN = InstallPlan(
    name="go",
    version="1.26.5",
    sha256="a" * 64,
    archive="tar.gz",
    target="/sandbox/.local/toolchains/go-1.26.5",
)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


RECORDED = {PLAN.target: PLAN.sha256}


class TestOpenShellInstaller:
    def test_check_runs_the_script_with_the_image_python(self):
        with mock.patch.object(
            sandbox, "_exec_python", return_value=_completed(0, "PRESENT\n")
        ) as ex:
            assert OpenShellInstaller(recorded=RECORDED).is_installed(PLAN) is True
        label, script, args = ex.call_args.args
        assert label == "toolchain check"
        assert script == INSTALL_SCRIPT
        assert args == ["check", PLAN.to_json(), "/sandbox/.local/toolchains"]

    def test_check_absent(self):
        with mock.patch.object(sandbox, "_exec_python", return_value=_completed(0, "ABSENT\n")):
            assert OpenShellInstaller(recorded=RECORDED).is_installed(PLAN) is False

    @pytest.mark.parametrize(
        "recorded",
        [None, {}, {PLAN.target: "b" * 64}, {"/sandbox/.local/toolchains/go-1.26.6": "a" * 64}],
    )
    def test_a_marker_without_the_host_record_is_not_trusted(self, recorded):
        # An earlier agent can plant a toolchain dir with a matching marker;
        # only what the host recorded for this sandbox is skipped.
        with mock.patch.object(
            sandbox, "_exec_python", return_value=_completed(0, "PRESENT\n")
        ) as ex:
            assert OpenShellInstaller(recorded=recorded).is_installed(PLAN) is False
        ex.assert_not_called()

    @pytest.mark.parametrize(
        "result", [_completed(4, "FAILED OSError\n"), _completed(0, "garbage\n"), _completed(0)]
    )
    def test_check_failure_raises(self, result):
        with mock.patch.object(sandbox, "_exec_python", return_value=result):
            with pytest.raises(ToolchainError, match="provisioning check failed; see the job log"):
                OpenShellInstaller(recorded=RECORDED).is_installed(PLAN)

    def test_install_uploads_then_extracts(self, tmp_path):
        archive = tmp_path / ("a" * 64)
        archive.write_bytes(b"x")
        calls = mock.Mock()
        calls._exec_python.return_value = _completed(0, "INSTALLED members=3\n")
        with (
            mock.patch.object(sandbox, "upload", calls.upload),
            mock.patch.object(sandbox, "_exec_python", calls._exec_python),
        ):
            OpenShellInstaller().install(PLAN, archive)
        assert [c[0] for c in calls.mock_calls] == ["upload", "_exec_python"]
        calls.upload.assert_called_once_with(str(archive), timeout=900)
        args = calls._exec_python.call_args.args[2]
        assert args == ["install", PLAN.to_json(), "/sandbox/.local/toolchains", "a" * 64]
        assert calls._exec_python.call_args.kwargs["timeout"] == 900

    def test_refusal_maps_to_a_fixed_reason(self, tmp_path, capsys):
        with (
            mock.patch.object(sandbox, "upload"),
            mock.patch.object(
                sandbox, "_exec_python", return_value=_completed(3, "REFUSED link\n", "trace")
            ),
        ):
            with pytest.raises(ToolchainError) as excinfo:
                OpenShellInstaller().install(PLAN, tmp_path / "x")
        assert str(excinfo.value) == (
            "go 1.26.5: an archive link points outside the target directory; nothing was installed"
        )
        logged = capsys.readouterr().out
        assert "toolchain install: REFUSED link" in logged
        assert "toolchain install stderr: trace" in logged

    @pytest.mark.parametrize(
        "result",
        [
            _completed(4, "FAILED PermissionError\n"),
            _completed(3, "REFUSED\n"),
            _completed(0, ""),
            _completed(1, "Traceback: secret"),
        ],
    )
    def test_other_failures_point_to_the_log(self, tmp_path, result):
        with (
            mock.patch.object(sandbox, "upload"),
            mock.patch.object(sandbox, "_exec_python", return_value=result),
        ):
            with pytest.raises(ToolchainError) as excinfo:
                OpenShellInstaller().install(PLAN, tmp_path / "x")
        assert "secret" not in str(excinfo.value)
        assert "see the job log" in str(excinfo.value) or "was refused" in str(excinfo.value)

    def test_upload_failure_discards_the_partial_upload(self, tmp_path):
        error = subprocess.CalledProcessError(1, ["openshell"], stderr="secret")
        with (
            mock.patch.object(sandbox, "upload", side_effect=error),
            mock.patch.object(
                sandbox, "_exec_python", return_value=_completed(0, "DISCARDED\n")
            ) as ex,
        ):
            with pytest.raises(ToolchainError, match="upload to the sandbox failed"):
                OpenShellInstaller().install(PLAN, tmp_path / ("a" * 64))
        assert ex.call_args.args[0] == "toolchain discard"
        assert ex.call_args.args[2] == ["discard", "a" * 64]

    def test_upload_timeout(self, tmp_path):
        with (
            mock.patch.object(
                sandbox, "upload", side_effect=subprocess.TimeoutExpired("openshell", 900)
            ),
            mock.patch.object(
                sandbox, "_exec_python", return_value=_completed(0, "DISCARDED\n")
            ) as ex,
        ):
            with pytest.raises(ToolchainError) as excinfo:
                OpenShellInstaller().install(PLAN, tmp_path / ("a" * 64))
        assert str(excinfo.value) == "go 1.26.5: upload to the sandbox timed out"
        assert ex.call_args.args[2] == ["discard", "a" * 64]

    def test_timeout(self, tmp_path):
        with (
            mock.patch.object(sandbox, "upload"),
            mock.patch.object(
                sandbox,
                "_exec_python",
                side_effect=[subprocess.TimeoutExpired("x", 900), _completed(0, "DISCARDED\n")],
            ) as ex,
        ):
            with pytest.raises(ToolchainError, match="extraction timed out in the sandbox"):
                OpenShellInstaller().install(PLAN, tmp_path / ("a" * 64))
        assert ex.call_args.args[2] == ["discard", "a" * 64]

    @pytest.mark.parametrize(
        "cleanup", [subprocess.TimeoutExpired("x", 60), _completed(4, "FAILED OSError\n")]
    )
    def test_a_failed_discard_keeps_the_original_error(self, tmp_path, cleanup):
        error = subprocess.CalledProcessError(1, ["openshell"])
        with (
            mock.patch.object(sandbox, "upload", side_effect=error),
            mock.patch.object(
                sandbox,
                "_exec_python",
                **(
                    {"side_effect": cleanup}
                    if isinstance(cleanup, Exception)
                    else {"return_value": cleanup}
                ),
            ),
        ):
            with pytest.raises(ToolchainError, match="upload to the sandbox failed"):
                OpenShellInstaller().install(PLAN, tmp_path / ("a" * 64))

    def test_refusals_are_not_discarded_again(self, tmp_path):
        # The script removes the upload itself once it ran.
        with (
            mock.patch.object(sandbox, "upload"),
            mock.patch.object(
                sandbox, "_exec_python", return_value=_completed(3, "REFUSED checksum\n")
            ) as ex,
        ):
            with pytest.raises(ToolchainError, match="does not match its sha256"):
                OpenShellInstaller().install(PLAN, tmp_path / ("a" * 64))
        assert ex.call_count == 1

    def test_every_refusal_has_a_reason(self):
        for reason in provision.REFUSALS:
            assert f'Refused("{reason}")' in INSTALL_SCRIPT
        for raised in set(re.findall(r'Refused\("([a-z-]+)"\)', INSTALL_SCRIPT)):
            assert raised in provision.REFUSALS


def test_the_script_compiles():
    compile(INSTALL_SCRIPT, "<install>", "exec")
