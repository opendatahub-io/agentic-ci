"""Toolchain installation inside the OpenShell sandbox.

The host resolves, downloads and verifies each toolchain archive
(:mod:`agentic_ci.toolchains`); :class:`OpenShellInstaller` uploads the
verified archive and extracts it inside the sandbox with
:data:`INSTALL_SCRIPT`, run by the image's own ``/usr/bin/python3``
(``tarfile`` and ``zipfile``: the Hummingbird sandbox images ship python3,
tar, gzip and xz but no unzip). Nothing is ever extracted on the host.

The script opens the uploaded archive once (no symlink, a regular file),
copies it into a fresh private directory while hashing it, and refuses it
unless that copy has the expected sha256; everything after reads the copy,
so swapping the upload by name cannot slip an unverified archive in. It then
checks every member before it extracts anything: no absolute path, no
``..``, no member under a symlink, no duplicate, no device, FIFO or other
special file, and every symlink and hard link must resolve inside the target
directory (links are resolved through the archive's own symlinks). It
extracts into a temporary directory next to the target, writes a marker with
the sha256 and renames it into place, so a failed extraction leaves nothing
half-installed. For an archive with ``components`` (Rust), only the members
under ``<component_root>/<component>/`` are extracted, links are refused
there, and each component's files are moved into the toolchain directory as
the tool's own installer would place them; a file two components both
provide is refused.

The marker only records what was installed; it is not an integrity check.
The toolchain directory is writable by the agent, so an earlier run on a
reused sandbox can plant a directory with a matching marker or change the
installed files. :class:`OpenShellInstaller` therefore skips a toolchain
only when the host's own record of this sandbox (kept in the saved sandbox
identity) names the same target and sha256 as the marker; otherwise it
installs again, which replaces the target directory. Files an earlier run
changed inside a recorded toolchain are not detected.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from agentic_ci import log
from agentic_ci.backends.openshell import sandbox
from agentic_ci.toolchains import SANDBOX_TOOLCHAIN_ROOT, InstallPlan, ToolchainError

# How long the upload, the in-sandbox check and the install may take (the Rust
# archive is about 210 MB, 650 MB once its components are merged; archives are
# at most 512 MiB).
_UPLOAD_TIMEOUT_SECONDS = 900
_CHECK_TIMEOUT_SECONDS = 60
_INSTALL_TIMEOUT_SECONDS = 900

MARKER = ".agentic-ci-toolchain.json"

# Exit statuses of INSTALL_SCRIPT.
_REFUSED = 3
_FAILED = 4

# The script's REFUSED classes and what they mean, for results and the log.
REFUSALS = {
    "target": "the target directory is not the expected one",
    "archive-name": "the uploaded archive name is not its sha256",
    "upload": "the uploaded archive is not a regular file",
    "checksum": "the uploaded archive does not match its sha256",
    "format": "the archive could not be read",
    "member-path": "an archive member points outside the target directory",
    "member-type": "an archive member is not a file, directory or link",
    "link": "an archive link points outside the target directory",
    "duplicate": "an archive member appears twice",
    "too-many-members": "the archive has too many members",
    "too-large": "the archive expands beyond the size limit",
    "name": "a launcher or binary name is invalid",
    "component": "the archive lacks a listed component",
}

INSTALL_SCRIPT = r"""
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile

MARKER = ".agentic-ci-toolchain.json"
MAX_MEMBERS = 200000
MAX_BYTES = 4 << 30
MAX_ARCHIVE_BYTES = 1 << 30
MAX_LINK_HOPS = 40
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SHA_RE = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    pass


def load_plan(plan_json, root):
    plan = json.loads(plan_json)
    for key in ("name", "version"):
        if not NAME_RE.fullmatch(plan[key]):
            raise Refused("target")
    if plan["target"] != os.path.join(root, plan["name"] + "-" + plan["version"]):
        raise Refused("target")
    if not SHA_RE.fullmatch(plan["sha256"]):
        raise Refused("target")
    return plan


def marker_data(plan):
    return {"name": plan["name"], "version": plan["version"], "sha256": plan["sha256"]}


def installed(plan):
    try:
        fd = os.open(os.path.join(plan["target"], MARKER), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    with os.fdopen(fd) as fh:
        try:
            return json.load(fh) == marker_data(plan)
        except ValueError:
            return False


def norm(name):
    # A member name as a clean relative path; "" is the archive root.
    if name.startswith("/") or "\0" in name:
        raise Refused("member-path")
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise Refused("member-path")
        parts.append(part)
    return "/".join(parts)


def resolve(path, links):
    # Resolve *path* through the archive's symlinks; refuse leaving the root.
    queue = [p for p in path.split("/") if p]
    out = []
    hops = 0
    while queue:
        part = queue.pop(0)
        if part == ".":
            continue
        if part == "..":
            if not out:
                raise Refused("link")
            out.pop()
            continue
        candidate = "/".join(out + [part])
        if candidate in links:
            hops += 1
            target = links[candidate]
            if hops > MAX_LINK_HOPS or target.startswith("/"):
                raise Refused("link")
            queue = [p for p in target.split("/") if p] + queue
            continue
        out.append(part)
    return "/".join(out)


def check_tar(members):
    if len(members) > MAX_MEMBERS:
        raise Refused("too-many-members")
    seen = set()
    links = {}
    files = set()
    total = 0
    for member in members:
        name = norm(member.name)
        if name == "":
            if member.isdir():
                continue
            raise Refused("member-path")
        if name in seen:
            raise Refused("duplicate")
        seen.add(name)
        if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
            raise Refused("member-type")
        if member.issym():
            links[name] = member.linkname
        elif member.isreg():
            files.add(name)
            total += member.size
            if total > MAX_BYTES:
                raise Refused("too-large")
    for name in seen:
        parts = name.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in links:
                raise Refused("member-path")
    for member in members:
        name = norm(member.name)
        if member.issym():
            if member.linkname.startswith("/"):
                raise Refused("link")
            resolve(os.path.dirname(name) + "/" + member.linkname, links)
        elif member.islnk():
            target = norm(member.linkname)
            if target not in files:
                raise Refused("link")


def extract_tar(archive, dest, prefixes=()):
    # With *prefixes*, only members at or under one of them are extracted.
    try:
        tar = tarfile.open(archive, "r:*")
    except (tarfile.TarError, OSError):
        raise Refused("format")
    with tar:
        members = tar.getmembers()
        check_tar(members)
        members = [m for m in members if norm(m.name) != ""]
        if prefixes:
            members = [
                m
                for m in members
                if any(norm(m.name) == p or norm(m.name).startswith(p + "/") for p in prefixes)
            ]
            if any(m.issym() or m.islnk() for m in members):
                raise Refused("link")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members=members, filter="data")
        else:
            tar.extractall(dest, members=members)
    return len(members)


def extract_zip(archive, dest):
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        raise Refused("format")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise Refused("too-many-members")
        seen = set()
        total = 0
        for info in infos:
            name = norm(info.filename)
            if name == "" and not info.is_dir():
                raise Refused("member-path")
            if name in seen:
                raise Refused("duplicate")
            seen.add(name)
            mode = info.external_attr >> 16
            if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise Refused("member-type")
            total += info.file_size
            if total > MAX_BYTES:
                raise Refused("too-large")
        count = 0
        for info in infos:
            name = norm(info.filename)
            path = os.path.join(dest, name) if name else dest
            if info.is_dir():
                os.makedirs(path, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            executable = (info.external_attr >> 16) & 0o111
            written = 0
            with zf.open(info) as src, open(path, "xb") as dst:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > info.file_size:
                        raise Refused("too-large")
                    dst.write(chunk)
            os.chmod(path, 0o755 if executable else 0o644)
            count += 1
    return count


def merge_components(plan, archive, dest):
    # Extract the listed components and move their files into *dest*, the
    # layout the tool's installer produces. Each component's manifest.in, the
    # installer's file list, is not installed.
    root = plan["component_root"]
    components = plan["components"]
    if not NAME_RE.fullmatch(root) or not all(NAME_RE.fullmatch(c) for c in components):
        raise Refused("name")
    stage = os.path.join(dest, ".agentic-ci-stage")
    os.mkdir(stage)
    extract_tar(archive, stage, [root + "/" + c for c in components])
    count = 0
    for component in components:
        source = os.path.join(stage, root, component)
        if not os.path.isdir(source) or os.path.islink(source):
            raise Refused("component")
        for dirpath, dirnames, filenames in os.walk(source):
            relative = os.path.relpath(dirpath, source)
            if relative.split("/")[0] == ".agentic-ci-stage":
                raise Refused("member-path")
            target_dir = os.path.normpath(os.path.join(dest, relative))
            if os.path.lexists(target_dir) and not os.path.isdir(target_dir):
                raise Refused("duplicate")
            os.makedirs(target_dir, exist_ok=True)
            for filename in filenames:
                if relative == "." and filename == "manifest.in":
                    continue
                if relative == "." and filename == MARKER:
                    raise Refused("member-path")
                target = os.path.join(target_dir, filename)
                if os.path.lexists(target):
                    raise Refused("duplicate")
                os.rename(os.path.join(dirpath, filename), target)
                count += 1
    shutil.rmtree(stage)
    return count


def private_copy(archive, work, expected):
    # Copy the upload through one descriptor, never following a symlink, into
    # the private *work* dir, hashing what is copied. Only that copy is used.
    try:
        fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Refused("upload")
    copy = os.path.join(work, "archive")
    digest = hashlib.sha256()
    size = 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused("upload")
        with os.fdopen(fd, "rb", closefd=False) as src, open(copy, "xb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise Refused("checksum")
                digest.update(chunk)
                dst.write(chunk)
    finally:
        os.close(fd)
    if digest.hexdigest() != expected:
        raise Refused("checksum")
    return copy


def write_launcher(path, text):
    with open(path, "x") as fh:
        fh.write(text)
    os.chmod(path, 0o755)


def install(plan, root, upload):
    if not SHA_RE.fullmatch(upload):
        raise Refused("archive-name")
    os.makedirs(root, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".upload-", dir=root)
    try:
        archive = private_copy(upload, work, plan["sha256"])
        return extract(plan, archive)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def extract(plan, archive):
    root = os.path.dirname(plan["target"])
    tmp = tempfile.mkdtemp(prefix="." + plan["name"] + "-", dir=root)
    try:
        wrappers = os.path.join(tmp, plan["wrapper_dir"])
        if plan["archive"] in ("tar.gz", "tar.xz") and plan.get("components"):
            count = merge_components(plan, archive, tmp)
        elif plan["archive"] in ("tar.gz", "tar.xz"):
            count = extract_tar(archive, tmp)
        elif plan["archive"] == "zip":
            count = extract_zip(archive, tmp)
        elif plan["archive"] == "binary":
            if not NAME_RE.fullmatch(plan["binary"]):
                raise Refused("name")
            os.makedirs(wrappers)
            shutil.copyfile(archive, os.path.join(wrappers, plan["binary"]))
            os.chmod(os.path.join(wrappers, plan["binary"]), 0o755)
            count = 1
        else:
            raise Refused("format")
        for name, script in sorted(plan["node_scripts"].items()):
            relative = norm(script)
            if not NAME_RE.fullmatch(name) or not relative:
                raise Refused("name")
            os.makedirs(wrappers, exist_ok=True)
            target = os.path.join(plan["target"], relative)
            write_launcher(
                os.path.join(wrappers, name),
                "#!/bin/sh\nexec node " + shlex.quote(target) + ' "$@"\n',
            )
        with open(os.path.join(tmp, MARKER), "x") as fh:
            json.dump(marker_data(plan), fh)
        os.chmod(tmp, 0o755)
        if os.path.islink(plan["target"]):
            os.unlink(plan["target"])
        elif os.path.lexists(plan["target"]):
            shutil.rmtree(plan["target"])
        os.rename(tmp, plan["target"])
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return count


def discard(upload):
    # Remove an upload that was never installed (a failed upload or install).
    if not SHA_RE.fullmatch(upload):
        print("REFUSED archive-name")
        return 3
    try:
        os.unlink(upload)
    except OSError:
        pass
    print("DISCARDED")
    return 0


def main(argv):
    mode = argv[1]
    if mode == "discard":
        return discard(argv[2])
    archive = argv[4] if mode == "install" else None
    try:
        plan = load_plan(argv[2], argv[3])
        if mode == "check":
            print("PRESENT" if installed(plan) else "ABSENT")
            return 0
        count = install(plan, argv[3], archive)
        print(f"INSTALLED members={count}")
        return 0
    except Refused as exc:
        print(f"REFUSED {exc.args[0]}")
        return 3
    except Exception as exc:
        print(f"FAILED {type(exc).__name__}")
        return 4
    finally:
        if archive is not None:
            try:
                os.unlink(archive)
            except OSError:
                pass


sys.exit(main(sys.argv))
"""


def _run_script(label: str, args: list[str], timeout: int, what: str) -> tuple[int, list[str]]:
    try:
        result = sandbox.exec_python(label, INSTALL_SCRIPT, args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ToolchainError(f"{what} timed out in the sandbox") from exc
    lines = (result.stdout or "").splitlines()
    for line in lines:
        log.detail(label, line)
    stderr = (result.stderr or "").strip()
    if stderr:
        log.detail(f"{label} stderr", stderr)
    return result.returncode, lines


class OpenShellInstaller:
    """:class:`agentic_ci.toolchains.Installer` for the OpenShell sandbox.

    *recorded* is the host's record of the toolchains this sandbox was given
    (target directory to sha256). :meth:`is_installed` is True only when that
    record and the in-sandbox marker both name the plan's sha256.
    """

    def __init__(
        self, root: str = SANDBOX_TOOLCHAIN_ROOT, *, recorded: Mapping[str, str] | None = None
    ) -> None:
        self.root = root
        self.recorded = dict(recorded or {})

    def is_installed(self, plan: InstallPlan) -> bool:
        if self.recorded.get(plan.target) != plan.sha256:
            # Whatever the sandbox holds there was not recorded by the host.
            return False
        what = f"{plan.name} {plan.version}: the provisioning check"
        returncode, lines = _run_script(
            "toolchain check", ["check", plan.to_json(), self.root], _CHECK_TIMEOUT_SECONDS, what
        )
        if returncode == 0 and lines[-1:] == ["PRESENT"]:
            return True
        if returncode == 0 and lines[-1:] == ["ABSENT"]:
            return False
        raise ToolchainError(f"{what} failed; see the job log")

    def install(self, plan: InstallPlan, archive: Path) -> None:
        what = f"{plan.name} {plan.version}"
        try:
            # Lands in the sandbox's working directory, where the script is run.
            sandbox.upload(str(archive), timeout=_UPLOAD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            self._discard(archive.name)
            raise ToolchainError(f"{what}: upload to the sandbox timed out") from exc
        except subprocess.CalledProcessError as exc:
            self._discard(archive.name)
            raise ToolchainError(f"{what}: upload to the sandbox failed; see the job log") from exc
        try:
            returncode, lines = _run_script(
                "toolchain install",
                ["install", plan.to_json(), self.root, archive.name],
                _INSTALL_TIMEOUT_SECONDS,
                f"{what}: extraction",
            )
        except ToolchainError:
            # The script did not finish, so it may not have removed the upload.
            self._discard(archive.name)
            raise
        last = lines[-1].split() if lines else []
        if returncode == 0 and last[:1] == ["INSTALLED"]:
            return
        if returncode == _REFUSED and len(last) == 2 and last[0] == "REFUSED":
            reason = REFUSALS.get(last[1], "the archive was refused")
            raise ToolchainError(f"{what}: {reason}; nothing was installed")
        raise ToolchainError(f"{what}: extraction in the sandbox failed; see the job log")

    @staticmethod
    def _discard(name: str) -> None:
        """Best effort: remove the uploaded archive *name* from the sandbox."""
        try:
            returncode, lines = _run_script(
                "toolchain discard", ["discard", name], _CHECK_TIMEOUT_SECONDS, "discard"
            )
        except Exception as exc:  # cleanup must not hide the original failure
            log.detail("toolchain discard failed", type(exc).__name__)
            return
        if returncode != 0 or lines[-1:] != ["DISCARDED"]:
            log.detail("toolchain discard failed", f"exit status {returncode}")
