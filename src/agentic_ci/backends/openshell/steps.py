"""Sandbox profile steps inside the OpenShell sandbox: setup, validate and discard.

A profile's ``setup`` steps and ``validate`` commands are repository code.
They run inside the sandbox through the setup shim
(:data:`sandbox.SANDBOX_SETUP_SHIM`), so the egress the setup and validate
phases open reaches them and nothing else, and never on the CI host.

:func:`run_step` starts each one with :data:`STEP_WRAPPER`, run by the image's
own ``/usr/bin/python3`` without a login shell, which:

- builds the step's environment from scratch: the variables agentic-ci passes
  (a minimal ``PATH`` and ``HOME``, the toolchain variables and the profile's
  ``env``) and then the proxy and CA variables OpenShell sets for every
  process (:data:`INHERITED_ENV`), which win, so nothing else the exec
  carries reaches the step and the LLM env script is never sourced;
- changes to the workdir and runs ``SHIM /usr/bin/bash -c RUN``; the shim
  puts the step in its own process group;
- becomes a child subreaper (``PR_SET_CHILD_SUBREAPER``), so what the step
  leaves behind (a background job, a double-forked daemon) is reparented to
  it rather than to the sandbox's init;
- enforces the step's timeout inside the sandbox: SIGTERM to the shim (which
  forwards it to the step's group) and every process below the wrapper, then
  SIGKILL after :data:`KILL_GRACE_SECONDS`, and exits 124;
- once the step's shim exits, on success, failure or timeout, SIGKILLs and
  reaps every process still below it, so no step process outlives its step
  or keeps its output open.

The host reads the combined output and keeps only its end; :func:`tail_text`
turns it into at most :data:`TAIL_LINES` redacted lines for the records. A
step that fails or times out is recorded, never raised.

:func:`wipe_files` deletes harness credential files before validate commands
run. :func:`discard_paths` moves ``discard_before_download`` paths out of the
workdir (never through a symlink) before the workdir is downloaded, and
:func:`restore_discarded` moves them back before the next run's agent.
"""

from __future__ import annotations

import json
import posixpath
import re
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from agentic_ci import log
from agentic_ci.backends.openshell import sandbox
from agentic_ci.redact import redact
from agentic_ci.toolchains import write_atomic

# Most lines and characters per line a record's tail keeps.
TAIL_LINES = 50
TAIL_LINE_CHARS = 400
# Bytes of output the host keeps while a step runs; only the end is recorded.
_TAIL_BUFFER_BYTES = 256 << 10

# How long a step gets between SIGTERM and SIGKILL once its timeout passes,
# and how much longer the host waits for the exec before it gives up on it.
KILL_GRACE_SECONDS = 10
_HOST_SLACK_SECONDS = 60

# Exit status of the wrapper when the step timed out (as coreutils timeout).
TIMEOUT_EXIT = 124

STEP_SHELL = "/usr/bin/bash"
STEP_HOME = "/sandbox"
# /sandbox/.local/bin first, as in the sandbox images' own PATH; toolchains
# come before it.
STEP_BASE_PATH = "/sandbox/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"

INHERITED_ENV = (
    # The CA bundle of the OpenShell proxy, which terminates TLS for the
    # L7-inspected preset hosts (OpenShell v0.1.2 sets these for every exec).
    "CURL_CA_BUNDLE",
    "DENO_CERT",
    "GIT_SSL_CAINFO",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    # Proxy settings, should a runtime set them (v0.1.2 routes transparently).
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    # Plain facts about the sandbox.
    "OPENSHELL_SANDBOX",
    "TERM",
    "USER",
)
"""Variables a step inherits from the exec when set: OpenShell's CA and proxy settings.

Without the CA variables a step would not trust the certificates OpenShell
presents for the L7-inspected preset hosts. Nothing else the exec carries
(the image's variables, a provider placeholder) is passed on.
"""

STEP_WRAPPER = r"""
import json
import os
import signal
import sys
import time

try:
    import ctypes
except ImportError:
    ctypes = None

PR_SET_CHILD_SUBREAPER = 36


def become_subreaper():
    # Orphans of the step (a background job, a double-forked daemon) are
    # reparented to this process instead of the sandbox's init, so they can
    # be found and reaped once the step ends.
    if ctypes is None:
        return
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except (AttributeError, OSError):
        pass


def descendants(root):
    children = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                fields = fh.read().rpartition(b")")[2].split()
        except OSError:
            continue
        if len(fields) > 1:
            children.setdefault(int(fields[1]), []).append(int(name))
    found, stack = [], [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            found.append(child)
            stack.append(child)
    return found


def signal_all(sig):
    for target in descendants(os.getpid()):
        try:
            os.kill(target, sig)
        except OSError:
            pass


def reap(pid):
    # Reap every child that exited; return the wait status of *pid* if it did.
    status = None
    while True:
        try:
            done, child_status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return status
        if done == 0:
            return status
        if done == pid:
            status = child_status


def kill_the_rest():
    # SIGKILL whatever the step left and reap it, so nothing outlives the
    # step or keeps its output open.
    for _ in range(200):
        signal_all(signal.SIGKILL)
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        time.sleep(0.05)


spec = json.loads(sys.argv[1])
argv = sys.argv[2:]
env = dict(spec["env"])
env.update({name: os.environ[name] for name in spec["inherit"] if name in os.environ})
try:
    os.chdir(spec["cwd"])
except OSError:
    print("agentic-ci: the workdir is missing in the sandbox", file=sys.stderr, flush=True)
    sys.exit(125)
become_subreaper()
sys.stdout.flush()
sys.stderr.flush()
pid = os.fork()
if pid == 0:
    try:
        os.execve(argv[0], argv, env)
    except OSError:
        os.write(2, b"agentic-ci: the setup shim could not be run\n")
    os._exit(127)
deadline = time.monotonic() + spec["timeout"]
kill_at = None
while True:
    status = reap(pid)
    if status is not None:
        break
    now = time.monotonic()
    if kill_at is None and now >= deadline:
        print(
            f"agentic-ci: step timed out after {spec['timeout']}s; stopping it",
            file=sys.stderr,
            flush=True,
        )
        signal_all(signal.SIGTERM)
        kill_at = now + spec["grace"]
    elif kill_at is not None and now >= kill_at:
        signal_all(signal.SIGKILL)
        kill_at = float("inf")
    time.sleep(0.1)
kill_the_rest()
if kill_at is not None:
    sys.exit(124)
code = os.waitstatus_to_exitcode(status)
sys.exit(code if code >= 0 else 128 - code)
"""

WIPE_SCRIPT = r"""
import os
import shutil
import sys

left = 0
for index, path in enumerate(sys.argv[1:]):
    if not path.startswith("/") or os.path.normpath(path) != path:
        print(f"REFUSED {index}")
        left += 1
        continue
    try:
        os.unlink(path)
    except IsADirectoryError:
        shutil.rmtree(path, ignore_errors=True)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    if os.path.lexists(path):
        print(f"LEFT {index}")
        left += 1
print(f"WIPED left={left}")
sys.exit(4 if left else 0)
"""

# Shared by DISCARD_SCRIPT and RESTORE_SCRIPT: walk a workdir-relative path
# without following a symlink, and remove a tree the same way.
_WALK_HELPERS = r"""
import json
import os
import stat
import sys

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MANIFEST = "manifest.json"


class Refused(Exception):
    pass


def remove_tree(parent_fd, name):
    # Every directory is opened relative to its parent without following a
    # symlink, so nothing outside the tree is ever reached.
    fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    try:
        with os.scandir(fd) as entries:
            children = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
        for child, is_dir in children:
            if is_dir:
                remove_tree(fd, child)
            else:
                os.unlink(child, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def remove_entry(parent_fd, name):
    info = os.lstat(name, dir_fd=parent_fd)
    if stat.S_ISDIR(info.st_mode):
        remove_tree(parent_fd, name)
    else:
        os.unlink(name, dir_fd=parent_fd)


def check(relative):
    parts = relative.split("/") if isinstance(relative, str) else []
    if (
        not parts
        or relative.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or ".git" in parts
    ):
        raise Refused()
    return parts


def parent_of(root_fd, parts):
    # The directory holding the last component, opened without following a
    # symlink; None when a directory on the way does not exist.
    fd = os.dup(root_fd)
    for part in parts[:-1]:
        try:
            next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None
        except OSError:
            # A symlink (ELOOP) or a file on the way: never followed.
            os.close(fd)
            raise Refused()
        os.close(fd)
        fd = next_fd
    return fd


def open_stash(stash, create):
    # The stash directory's parent and the stash itself, never through a symlink.
    if not os.path.isabs(stash) or os.path.normpath(stash) != stash:
        raise OSError("the stash must be an absolute, normalized path")
    parent, name = os.path.split(stash)
    if create:
        try:
            os.mkdir(parent, 0o755)
        except FileExistsError:
            pass
    parent_fd = os.open(parent, DIR_FLAGS)
    if create:
        try:
            remove_entry(parent_fd, name)
        except FileNotFoundError:
            pass
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    return parent_fd, os.open(name, DIR_FLAGS, dir_fd=parent_fd), name


def open_root(root):
    try:
        return os.open(root, DIR_FLAGS)
    except OSError:
        print("ROOT missing")
        sys.exit(3)
"""

DISCARD_SCRIPT = (
    _WALK_HELPERS
    + r"""

root_fd = open_root(sys.argv[1])
try:
    stash_parent_fd, stash_fd, _ = open_stash(sys.argv[2], create=True)
except OSError:
    # No usable stash (a symlink the agent planted, say): remove instead.
    print("STASH unavailable")
    stash_fd = None
manifest = {}


def discard(index, relative):
    try:
        parts = check(relative)
        fd = parent_of(root_fd, parts)
    except Refused:
        return "refused"
    if fd is None:
        return "absent"
    name = parts[-1]
    try:
        info = os.lstat(name, dir_fd=fd)
        if stash_fd is None or stat.S_ISLNK(info.st_mode):
            # A symlink is removed itself; its target is never touched.
            remove_entry(fd, name)
        else:
            os.rename(name, str(index), src_dir_fd=fd, dst_dir_fd=stash_fd)
            manifest[str(index)] = relative
        return "removed"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "failed"
    finally:
        os.close(fd)


for index, relative in enumerate(sys.argv[3:]):
    print(f"DISCARD {index} {discard(index, relative)}")
if stash_fd is not None:
    fd = os.open(MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=stash_fd)
    with os.fdopen(fd, "w") as fh:
        json.dump(manifest, fh)
print("DONE")
"""
)

RESTORE_SCRIPT = (
    _WALK_HELPERS
    + r"""

root_fd = open_root(sys.argv[1])
try:
    stash_parent_fd, stash_fd, stash_name = open_stash(sys.argv[2], create=False)
except OSError:
    print("RESTORED 0")
    sys.exit(0)
try:
    fd = os.open(MANIFEST, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stash_fd)
    with os.fdopen(fd) as fh:
        manifest = json.loads(fh.read(1 << 20))
except (OSError, ValueError):
    manifest = {}
restored = 0
for key, relative in (manifest.items() if isinstance(manifest, dict) else ()):
    if not (isinstance(key, str) and key.isdigit()):
        continue
    try:
        parts = check(relative)
        fd = parent_of(root_fd, parts)
    except Refused:
        continue
    if fd is None:
        continue
    try:
        os.lstat(parts[-1], dir_fd=fd)
    except FileNotFoundError:
        try:
            os.rename(key, parts[-1], src_dir_fd=stash_fd, dst_dir_fd=fd)
            restored += 1
        except OSError:
            pass
    finally:
        os.close(fd)
os.close(stash_fd)
remove_tree(stash_parent_fd, stash_name)
print(f"RESTORED {restored}")
"""
)

ENVIRONMENT_SCRIPT = r"""
import os
import shutil
import stat
import sys

source = sys.argv[1]
target = sys.argv[2]
names = sys.argv[3:]
info = os.lstat(source)
if not stat.S_ISDIR(info.st_mode):
    print("REFUSED upload")
    sys.exit(3)
if os.path.islink(target) or (os.path.lexists(target) and not os.path.isdir(target)):
    os.unlink(target)
os.makedirs(target, mode=0o755, exist_ok=True)
for name in names:
    destination = os.path.join(target, name)
    if os.path.isdir(destination) and not os.path.islink(destination):
        shutil.rmtree(destination)
    os.replace(os.path.join(source, name), destination)
shutil.rmtree(source, ignore_errors=True)
print("WRITTEN")
"""

_WIPE_TIMEOUT_SECONDS = 60
_DISCARD_TIMEOUT_SECONDS = 600
ENVIRONMENT_TIMEOUT_SECONDS = 60

# ANSI escape sequences and every other control character but tab.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Longest step name shown in the job log.
_MAX_SHOWN_NAME = 64


def shown_name(name: str) -> str:
    """A step name for a log line: the validated name, cut to a bounded length."""
    return name if len(name) <= _MAX_SHOWN_NAME else name[:_MAX_SHOWN_NAME] + "..."


@dataclass(frozen=True)
class StepRecord:
    """The outcome of one setup step or validate command, as the run records hold it.

    ``status`` is ``passed``, ``failed`` (a non-zero exit), ``timeout``,
    ``error`` (it could not be started) or ``not_run`` (its phase could not
    be opened; ``reason`` says so). ``kind`` is set for validate commands
    only. ``tail`` is the redacted end of the combined output.
    """

    name: str
    status: str
    rc: int | None = None
    seconds: float = 0.0
    tail: str = ""
    kind: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name}
        if self.kind is not None:
            data["kind"] = self.kind
        data.update(
            {"status": self.status, "rc": self.rc, "seconds": self.seconds, "tail": self.tail}
        )
        if self.reason:
            data["reason"] = self.reason
        return data

    @classmethod
    def from_dict(cls, data: object) -> StepRecord | None:
        """Read a :meth:`to_dict` mapping back, or return None if it is not one."""
        if not isinstance(data, dict):
            return None
        name, status, rc = data.get("name"), data.get("status"), data.get("rc")
        seconds, tail = data.get("seconds", 0.0), data.get("tail", "")
        kind, reason = data.get("kind"), data.get("reason", "")
        if not (
            isinstance(name, str)
            and isinstance(status, str)
            and (rc is None or type(rc) is int)
            and isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and isinstance(tail, str)
            and (kind is None or isinstance(kind, str))
            and isinstance(reason, str)
        ):
            return None
        return cls(name, status, rc, float(seconds), tail, kind, reason)


def build_env(
    path: Sequence[str], variables: Mapping[str, str], profile_env: Mapping[str, str]
) -> dict[str, str]:
    """The environment a step runs with, apart from :data:`INHERITED_ENV`.

    A minimal ``PATH`` with the toolchain directories (*path*) first,
    ``HOME``, ``LANG``, the toolchain *variables* and the profile's
    *profile_env* (which wins over a toolchain variable of the same name).
    Profile validation rejects ``PATH``, ``HOME`` and every
    :data:`INHERITED_ENV` name, and the wrapper applies the inherited values
    last anyway. No LLM, forge or other credential is ever in it.
    """
    env = {
        "HOME": STEP_HOME,
        "LANG": "C.UTF-8",
        "PATH": ":".join([*path, STEP_BASE_PATH]),
    }
    env.update(variables)
    env.update(profile_env)
    return env


class _Tail:
    """Keeps the last :data:`_TAIL_BUFFER_BYTES` of a stream."""

    def __init__(self, limit: int = _TAIL_BUFFER_BYTES) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def drain(self, stream: IO[bytes]) -> None:
        while True:
            chunk = stream.read(1 << 16)
            if not chunk:
                return
            self.data.extend(chunk)
            if len(self.data) > self.limit:
                del self.data[: len(self.data) - self.limit]
                self.truncated = True


def tail_text(data: bytes, secrets: Iterable[str] = (), *, truncated: bool = False) -> str:
    """The last :data:`TAIL_LINES` lines of *data*, redacted and cleaned for a record.

    ANSI escapes and control characters are removed, every line is redacted
    (see :func:`agentic_ci.redact.redact`, with the host's *secrets*) and then
    cut to :data:`TAIL_LINE_CHARS`. With *truncated*, the first line is a
    fragment of a longer one and is dropped, so half a secret cannot pass.
    """
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if truncated and lines:
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    secrets = tuple(secrets)
    kept = []
    for line in lines[-TAIL_LINES:]:
        line = _CONTROL_RE.sub("", _ANSI_RE.sub("", line))
        line = redact(line, secrets)
        if len(line) > TAIL_LINE_CHARS:
            line = line[:TAIL_LINE_CHARS] + "..."
        kept.append(line)
    return "\n".join(kept)


def _status(rc: int, seconds: float, timeout: int) -> str:
    if rc == 0:
        return "passed"
    # The wrapper exits 124 once it stopped the step; a step can exit 124 too,
    # so the time decides.
    if rc == TIMEOUT_EXIT and seconds >= timeout:
        return "timeout"
    return "failed"


def run_step(
    name: str,
    run: str,
    timeout: int,
    *,
    env: Mapping[str, str],
    cwd: str,
    secrets: Iterable[str] = (),
    kind: str | None = None,
) -> StepRecord:
    """Run one setup step or validate command in the sandbox and record it.

    *env* comes from :func:`build_env`; *cwd* is the sandbox workdir. The
    step runs under the setup shim with *timeout* seconds, enforced inside
    the sandbox; the host gives up on the exec :data:`KILL_GRACE_SECONDS` plus
    a minute later. Never raises for a failing step: every outcome is a
    :class:`StepRecord`.
    """
    spec = json.dumps(
        {
            "cwd": cwd,
            "env": dict(env),
            "grace": KILL_GRACE_SECONDS,
            "inherit": list(INHERITED_ENV),
            "timeout": timeout,
        },
        sort_keys=True,
    )
    args = sandbox.python_exec_args(
        STEP_WRAPPER, [spec, sandbox.SANDBOX_SETUP_SHIM, STEP_SHELL, "-c", run]
    )
    label = "validate command" if kind is not None else "setup step"
    log.detail("exec", f"<{label} {shown_name(name)} under {sandbox.SANDBOX_SETUP_SHIM}>")
    secrets = tuple(secrets)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
    except OSError as exc:
        log.detail(f"{label} error", type(exc).__name__)
        return StepRecord(
            name, "error", kind=kind, reason="the step could not be started; see the job log"
        )
    tail = _Tail()
    reader = threading.Thread(target=tail.drain, args=(proc.stdout,), daemon=True)
    reader.start()
    rc: int | None
    try:
        rc = proc.wait(timeout=timeout + KILL_GRACE_SECONDS + _HOST_SLACK_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        rc = None
    reader.join(timeout=5)
    if proc.stdout is not None and not reader.is_alive():
        proc.stdout.close()
    seconds = round(time.monotonic() - start, 1)
    text = tail_text(bytes(tail.data), secrets, truncated=tail.truncated)
    if rc is None:
        record = StepRecord(
            name,
            "timeout",
            None,
            seconds,
            text,
            kind,
            "the sandbox exec did not return in time; see the job log",
        )
    else:
        record = StepRecord(name, _status(rc, seconds, timeout), rc, seconds, text, kind)
    shown_rc = "-" if record.rc is None else str(record.rc)
    log.info(
        f"{label.capitalize()} {shown_name(name)}: {record.status} (exit {shown_rc}, {seconds}s)"
    )
    if text:
        log.detail(f"{label} {shown_name(name)} output tail", "\n" + text)
    return record


def wipe_files(paths: Sequence[str]) -> None:
    """Delete *paths* (absolute, normalized) in the sandbox and confirm they are gone.

    Raises ``RuntimeError`` (a class, no path) when the wipe cannot be
    confirmed, so the caller does not run validate commands.
    """
    if not paths:
        return
    what = "Could not delete the harness credential files before validation"
    try:
        result = sandbox.exec_python(
            "credential wipe", WIPE_SCRIPT, list(paths), timeout=_WIPE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{what}: the in-sandbox wipe timed out") from exc
    lines = (result.stdout or "").splitlines()
    for line in lines:
        log.detail("credential wipe", line)
    if result.returncode != 0 or "WIPED left=0" not in lines:
        stderr = (result.stderr or "").strip()
        if stderr:
            log.detail("credential wipe stderr", stderr)
        raise RuntimeError(f"{what}: exit status {result.returncode}; see the job log")
    log.info(f"Deleted {len(paths)} harness credential file path(s) before validation")


@dataclass(frozen=True)
class DiscardRecord:
    """One ``discard_before_download`` path and what happened to it.

    ``status`` is ``removed``, ``absent``, ``refused`` (the path leaves the
    workdir or crosses a symlink), ``failed`` or ``error`` (the removal did
    not report back).
    """

    path: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "status": self.status}


_DISCARD_STATUSES = frozenset({"removed", "absent", "refused", "failed"})


DISCARD_STASH = "/sandbox/.agentic-ci/discarded"
"""Where :func:`discard_paths` moves discarded paths, outside the workdir.

The next ``run()`` on the same sandbox moves them back (:func:`restore_discarded`)
before the agent starts, so a later agent run (after a classifier run or a
retry) still has what the setup steps installed.
"""


def discard_paths(workdir: str, paths: Sequence[str]) -> list[DiscardRecord]:
    """Move each workdir-relative path in *paths* out of *workdir* inside the sandbox.

    Each path goes to :data:`DISCARD_STASH`, which is outside the workdir and
    never downloaded, and comes back at the next run (see
    :func:`restore_discarded`); a symlink is removed itself, and without a
    usable stash (a symlink planted there) paths are deleted instead. Every
    path component is opened without following symlinks, so a symlink
    anywhere in the path (planted by the agent or a step) makes that path
    ``refused`` instead of reaching outside the workdir. ``..``, absolute
    paths and ``.git`` are refused. Failures are recorded, never raised.
    """
    if not paths:
        return []
    clean = [p if posixpath.normpath(p) == p else "" for p in paths]
    try:
        result = sandbox.exec_python(
            "discard before download",
            DISCARD_SCRIPT,
            [workdir, DISCARD_STASH, *clean],
            timeout=_DISCARD_TIMEOUT_SECONDS,
        )
        lines = (result.stdout or "").splitlines()
    except subprocess.TimeoutExpired:
        log.detail("discard before download", "timed out")
        lines = []
    statuses: dict[int, str] = {}
    for line in lines:
        parts = line.split()
        if (
            len(parts) == 3
            and parts[0] == "DISCARD"
            and parts[1].isdigit()
            and parts[2] in _DISCARD_STATUSES
        ):
            statuses[int(parts[1])] = parts[2]
        else:
            log.detail("discard before download", line)
    records = [DiscardRecord(path, statuses.get(i, "error")) for i, path in enumerate(paths)]
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    log.info(f"Discarded before download: {summary}")
    return records


def restore_discarded(workdir: str) -> int | None:
    """Move the paths the previous run discarded back into *workdir*; return how many.

    A path whose place in the workdir is taken, or whose parent directory is
    gone or crosses a symlink, is not restored; the stash is deleted either
    way. Returns None when the restore did not report back. Never raises.
    """
    try:
        result = sandbox.exec_python(
            "restore discarded paths",
            RESTORE_SCRIPT,
            [workdir, DISCARD_STASH],
            timeout=_DISCARD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.detail("restore discarded paths", "timed out")
        return None
    lines = (result.stdout or "").splitlines()
    parts = lines[-1].split() if lines else []
    if result.returncode == 0 and len(parts) == 2 and parts[0] == "RESTORED":
        if parts[1].isdigit():
            count = int(parts[1])
            if count:
                log.info(f"Restored {count} path(s) discarded by the previous run")
            return count
    stderr = (result.stderr or "").strip()
    if stderr:
        log.detail("restore discarded paths stderr", stderr)
    log.detail("restore discarded paths", f"exit status {result.returncode}")
    return None


def install_environment_files(uploaded: str, target: str, names: Sequence[str]) -> bool:
    """Move the uploaded directory's *names* into *target* in the sandbox; return success.

    *target* is replaced if it is a symlink or not a directory, and each file
    is renamed into place, so a link the agent left there is replaced rather
    than written through.
    """
    try:
        result = sandbox.exec_python(
            "environment files",
            ENVIRONMENT_SCRIPT,
            [uploaded, target, *names],
            timeout=ENVIRONMENT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.detail("environment files", "timed out")
        return False
    lines = (result.stdout or "").splitlines()
    if result.returncode == 0 and lines[-1:] == ["WRITTEN"]:
        return True
    stderr = (result.stderr or "").strip()
    if stderr:
        log.detail("environment files stderr", stderr)
    log.detail("environment files", f"exit status {result.returncode}")
    return False


def write_json(path: Path, data: object) -> None:
    """Write *data* as indented JSON to *path* atomically.

    The temporary file gets a fresh random name (``mkstemp``), so nothing left
    in the directory (a symlink the agent planted) is written through.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
