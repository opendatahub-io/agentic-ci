"""Generic git operations for CI pipelines.

Host-side git operations: clone, push, branch creation, diff inspection.
All operations use subprocess calls to git.
"""

from __future__ import annotations

import fnmatch
import logging
import math
import os
import re
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset({"github.com", "gitlab.com"})
GIT_CLONE_TIMEOUT = int(os.environ.get("GIT_CLONE_TIMEOUT", "300"))
GIT_PUSH_TIMEOUT = int(os.environ.get("GIT_PUSH_TIMEOUT", "120"))


def _safe_int(value: str, default: int) -> int:
    """Parse an integer string, returning *default* on invalid input."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value: str, default: float) -> float:
    """Parse a float string, returning *default* on invalid input."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


GIT_PUSH_MAX_RETRIES = _safe_int(os.environ.get("GIT_PUSH_MAX_RETRIES", "2"), 2)
GIT_PUSH_RETRY_DELAY = _safe_float(os.environ.get("GIT_PUSH_RETRY_DELAY", "5"), 5.0)
_TRANSIENT_PUSH_PATTERNS = (
    "fatal error in commit_refs",
    "cannot lock ref",
    "Connection refused",
    "Connection reset",
    "Connection timed out",
    "Could not resolve host",
    "SSL_connect",
    "The remote end hung up unexpectedly",
    "Service Unavailable",
    "Internal Server Error",
    "returned error: 500",
    "returned error: 502",
    "returned error: 503",
    "returned error: 504",
)

_DEVNULL = subprocess.DEVNULL


_GITLAB_URL_RE = re.compile(
    r"https://gitlab\.com/[a-zA-Z0-9/_.-]+",
    re.IGNORECASE,
)
_GITHUB_URL_RE = re.compile(
    r"https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+",
    re.IGNORECASE,
)
_SUBPATH_RE = re.compile(
    r"/-/(merge_requests|issues|blob|tree|raw|commits|pipelines|jobs)/|"
    r"/(pull|issues|blob|tree|raw|commits|actions|releases)/",
)
_FILE_EXT_RE = re.compile(r"\.(md|txt|py|sh|yml|yaml|json)$")
_PLACEHOLDER_RE = re.compile(r"(your-org|your-repo|example|placeholder)", re.IGNORECASE)


def _clean_url(url: str) -> str:
    return url.rstrip("/").removesuffix(".git")


def _is_placeholder(url: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(url))


def _collect_candidates(text: str, pattern: re.Pattern) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for m in pattern.finditer(text):
        url = _clean_url(m.group(0))
        if _SUBPATH_RE.search(url):
            idx = url.find("/-/")
            if idx != -1:
                url = url[:idx]
            else:
                continue
        if url in seen:
            continue
        if _FILE_EXT_RE.search(url):
            continue
        if _is_placeholder(url):
            continue
        seen.add(url)
        candidates.append(url)
    return candidates


def _validate_gitlab_url(url: str) -> bool:
    token = os.environ.get("BOT_PAT") or os.environ.get("GITLAB_TOKEN")
    if not token:
        return False
    repo_path = url.split("gitlab.com/", 1)[-1]
    encoded = urlquote(repo_path, safe="")
    try:
        req = Request(f"https://gitlab.com/api/v4/projects/{encoded}")
        req.add_header("PRIVATE-TOKEN", token)
        with urlopen(req, timeout=10):
            return True
    except (HTTPError, URLError, OSError):
        return False


def _validate_github_url(url: str) -> bool:
    repo_path = url.split("github.com/", 1)[-1]
    try:
        req = Request(f"https://api.github.com/repos/{repo_path}")
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urlopen(req, timeout=10):
            return True
    except (HTTPError, URLError, OSError):
        return False


def extract_repo_url(text: str) -> str | None:
    """Extract a repo URL from text, validating against forge APIs.

    Filters out subpaths, file extensions, and placeholder URLs.
    Returns the first URL that resolves to a real project, or the first
    unvalidated candidate if no API tokens are available.
    """
    candidates = _collect_candidates(text, _GITLAB_URL_RE)
    has_token = bool(os.environ.get("BOT_PAT") or os.environ.get("GITLAB_TOKEN"))
    if candidates and has_token:
        for url in candidates:
            if _validate_gitlab_url(url):
                return url
    if candidates:
        return candidates[0]

    candidates = _collect_candidates(text, _GITHUB_URL_RE)
    if candidates:
        for url in candidates:
            if _validate_github_url(url):
                return url
        return candidates[0]

    return None


def _dedup_gitlab_prefixes(urls: list[str]) -> list[str]:
    """Remove GitLab URLs that are strict prefixes of other GitLab URLs.

    GitLab nested-group URLs (e.g. ``gitlab.com/group``) can be strict
    prefixes of actual repo URLs (``gitlab.com/group/project``).  Keep
    only the longest match when one URL is a prefix of another.
    """
    if len(urls) <= 1:
        return urls
    longest_first = sorted(urls, key=len, reverse=True)
    kept: list[str] = []
    for url in longest_first:
        if not any(longer.startswith(url + "/") for longer in kept):
            kept.append(url)
    kept_set = set(kept)
    return [u for u in urls if u in kept_set]


def extract_all_repo_urls(text: str) -> list[str]:
    """Extract all distinct repo root URLs from text.

    Scans for both GitLab and GitHub URLs, filters out subpaths, file
    extensions, and placeholder URLs.  GitLab URLs that are strict
    prefixes of other GitLab URLs are collapsed (nested group dedup).

    Unlike :func:`extract_repo_url`, this does **not** validate URLs
    against forge APIs -- it returns all plausible candidates.
    """
    gitlab_urls = _dedup_gitlab_prefixes(_collect_candidates(text, _GITLAB_URL_RE))
    github_urls = _collect_candidates(text, _GITHUB_URL_RE)
    seen: set[str] = set()
    result: list[str] = []
    for url in gitlab_urls + github_urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def validate_repo_url(url: str) -> bool:
    """Check that a repo URL points to an allowed host with no path traversal."""
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    if not parsed.hostname or parsed.hostname not in ALLOWED_HOSTS:
        return False
    if parsed.username or parsed.password:
        return False
    if ".." in (parsed.path or ""):
        return False
    return True


_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/\-~^]+$")


def _validate_ref(name: str) -> bool:
    """Validate a git ref name against injection attacks."""
    if not name or name.startswith("-"):
        return False
    if ".." in name or "@{" in name:
        return False
    return bool(_SAFE_REF_RE.match(name))


def validate_branch_exists(repo_url: str, branch: str) -> bool:
    """Check if a branch exists on the remote repository.

    Args:
        repo_url: HTTPS URL of the git repository
        branch: Branch name to validate

    Returns:
        True if the branch exists on the remote, False otherwise

    Note:
        Returns False for any error condition (network issues, invalid refs, etc.)
        to allow graceful fallback in the resolution chain.
    """
    if not _validate_ref(branch):
        log.warning("Invalid branch name rejected: %s", branch)
        return False

    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", repo_url, branch],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=_DEVNULL,
        )

        if result.returncode != 0:
            log.debug(
                "git ls-remote failed for %s branch %s: %s", repo_url, branch, result.stderr.strip()
            )
            return False

        output = result.stdout.strip()
        if not output:
            log.debug("Branch %s does not exist on remote %s", branch, repo_url)
            return False

        log.debug("Branch %s exists on remote %s", branch, repo_url)
        return True

    except subprocess.TimeoutExpired:
        log.warning("Branch validation timed out for %s branch %s", repo_url, branch)
        return False
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.debug("Branch validation failed for %s branch %s: %s", repo_url, branch, exc)
        return False


def clone_repo(url: str, dest: Path, branch: str | None = None, depth: int | None = None) -> bool:
    """Clone a repository. Returns True on success."""
    if not validate_repo_url(url):
        log.error("clone_repo: invalid or disallowed URL: %s", url)
        return False
    if branch and not _validate_ref(branch):
        log.error("clone_repo: invalid branch name: %s", branch)
        return False
    cmd = [
        "git",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.file.allow=never",
        "clone",
    ]
    if depth:
        cmd += ["--depth", str(depth)]
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--", url, str(dest)]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_CLONE_TIMEOUT,
            stdin=_DEVNULL,
        )
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", str(dest.resolve())],
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.TimeoutExpired:
        log.error("git clone timed out after %ds for %s", GIT_CLONE_TIMEOUT, url)
        return False
    except subprocess.CalledProcessError as exc:
        log.error("git clone failed: %s", exc.stderr)
        return False


def create_branch(repo_dir: Path, branch_name: str) -> bool:
    """Create and checkout a new branch."""
    if not _validate_ref(branch_name):
        log.error("create_branch: invalid branch name: %s", branch_name)
        return False
    try:
        subprocess.run(
            ["git", "switch", "-c", branch_name],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        log.error("git switch -c failed: %s", exc.stderr)
        return False


def checkout_branch(repo_dir: Path, branch: str) -> bool:
    """Checkout an existing branch. Returns True on success."""
    if not _validate_ref(branch):
        log.error("checkout_branch: invalid branch name: %s", branch)
        return False
    try:
        subprocess.run(
            ["git", "checkout", branch],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
            stdin=_DEVNULL,
        )
        return True
    except subprocess.CalledProcessError as exc:
        log.error("git checkout failed: %s", exc.stderr)
        return False
    except FileNotFoundError:
        log.error("git binary not found")
        return False


def rebase_branch(repo_dir: Path, onto: str) -> bool:
    """Rebase the current branch onto *onto*. Returns True on success.

    On conflict the rebase is aborted so the worktree stays clean.
    """
    if not _validate_ref(onto):
        log.error("rebase_branch: invalid ref: %s", onto)
        return False
    try:
        subprocess.run(
            ["git", "rebase", onto],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
            stdin=_DEVNULL,
        )
        return True
    except subprocess.CalledProcessError as exc:
        log.error("git rebase failed: %s", exc.stderr)
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            stdin=_DEVNULL,
        )
        return False
    except FileNotFoundError:
        log.error("git binary not found")
        return False


def get_default_branch(repo_dir: Path) -> str:
    """Detect the default branch of the remote origin.

    Runs ``git rev-parse --abbrev-ref origin/HEAD`` and strips the
    ``origin/`` prefix. Falls back to ``"main"`` when the remote HEAD
    cannot be determined.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
            stdin=_DEVNULL,
        )
        ref = result.stdout.strip()
        if ref and ref != "origin/HEAD":
            return ref.removeprefix("origin/")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return "main"


def git_output(repo_dir: Path, *args: str) -> str | None:
    """Run a git command and return its stripped stdout, or None on error.

    This is a thin wrapper around ``subprocess.run`` for cases where
    the caller only needs the text output of a git command.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
            stdin=_DEVNULL,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


_SAFE_REMOTE_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _is_transient_push_error(stderr: str) -> bool:
    """Check whether a push failure stderr matches a known transient pattern."""
    lower = stderr.lower()
    return any(pat.lower() in lower for pat in _TRANSIENT_PUSH_PATTERNS)


class _TransientPushError(Exception):
    """Transient push failure that may succeed on retry."""


def push_branch(
    repo_dir: Path,
    remote: str = "origin",
    branch: str | None = None,
    *,
    max_retries: int = GIT_PUSH_MAX_RETRIES,
    retry_delay: float = GIT_PUSH_RETRY_DELAY,
) -> bool:
    """Push the current branch to remote. Returns True on success.

    Retries up to *max_retries* times on transient errors (server 5xx,
    ``commit_refs`` failures, lock contention, network resets) with
    exponential backoff starting at *retry_delay* seconds.
    """
    if not remote or remote.startswith("-") or ".." in remote or "@{" in remote:
        log.error("push_branch: invalid remote name: %s", remote)
        return False
    if not _SAFE_REMOTE_RE.match(remote):
        log.error("push_branch: invalid remote name: %s", remote)
        return False
    if branch and not _validate_ref(branch):
        log.error("push_branch: invalid branch name: %s", branch)
        return False
    if not branch:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_dir),
                check=True,
                capture_output=True,
                text=True,
                stdin=_DEVNULL,
            )
            branch = result.stdout.strip()
        except subprocess.CalledProcessError:
            log.error("push_branch: could not detect current branch")
            return False
        if not _validate_ref(branch):
            log.error("push_branch: detected invalid branch name: %s", branch)
            return False
    max_retries = max(0, max_retries)
    if not math.isfinite(retry_delay) or retry_delay < 0:
        retry_delay = 5.0

    cmd = ["git", "push", "--force-with-lease", "--set-upstream", remote, branch]
    total_attempts = 1 + max_retries

    @retry(
        stop=stop_after_attempt(total_attempts),
        wait=wait_exponential(multiplier=retry_delay, exp_base=2, min=0),
        retry=retry_if_exception_type(_TransientPushError),
        sleep=time.sleep,
        reraise=True,
        before_sleep=lambda rs: log.warning(
            "git push failed (attempt %d/%d), retrying in %.0fs: %s",
            rs.attempt_number,
            total_attempts,
            rs.next_action.sleep if rs.next_action else 0,
            str(rs.outcome.exception()) if rs.outcome else "unknown",
        ),
    )
    def _do_push() -> None:
        try:
            subprocess.run(
                cmd,
                cwd=str(repo_dir),
                check=True,
                capture_output=True,
                text=True,
                timeout=GIT_PUSH_TIMEOUT,
                stdin=_DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise _TransientPushError("git push timed out")
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            if _is_transient_push_error(stderr):
                raise _TransientPushError(stderr.strip()) from exc
            log.error("git push failed: %s", stderr.strip())
            raise

    try:
        _do_push()
        return True
    except _TransientPushError as exc:
        log.error("git push failed: %s", str(exc))
        return False
    except subprocess.CalledProcessError:
        return False


def setup_git_config(repo_dir: Path, name: str, email: str) -> None:
    """Set local git user config."""
    subprocess.run(
        ["git", "config", "user.name", name],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", email],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    )


def harden_git_config(repo_dir: Path) -> None:
    """Apply security hardening to git config (disable hooks, fsmonitor)."""
    for key, value in [
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
    ]:
        subprocess.run(
            ["git", "config", key, value],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
        )


# -- Host git control files ---------------------------------------------------

# Paths inside ``.git`` that decide what host-side git executes: ``config`` and
# ``config.worktree`` hold every command-running key (hooksPath, fsmonitor,
# sshCommand, pager, credential helpers, filter and diff drivers, aliases,
# include paths, insteadOf rewrites), ``commondir`` makes git read its config
# and hooks from another directory, ``hooks/`` holds hook scripts, and ``info/``
# holds ``attributes``, which binds files to filter and diff drivers. Objects,
# refs and the index are data and are kept, so the agent's commits survive.
GIT_CONTROL_PATHS = ("config", "config.worktree", "commondir", "hooks", "info")


class GitControlTamperError(RuntimeError):
    """The agent replaced ``.git`` itself, so the host copy cannot be restored."""


@dataclass(frozen=True)
class _ControlEntry:
    kind: str  # "file", "dir" or "symlink"
    mode: int = 0
    data: bytes = b""
    # Ownership is put back when restoring as root (the Podman backend chowns
    # the workdir to the container user) but is not a change the agent made.
    uid: int = field(default=-1, compare=False)
    gid: int = field(default=-1, compare=False)


@dataclass(frozen=True)
class GitControlSnapshot:
    """Host copy of the ``.git`` files that decide what host-side git executes.

    Taken by :func:`snapshot_git_control` before an agent can write the
    repository and put back by :func:`restore_git_control` afterwards.
    """

    repo_dir: Path
    dot_git: _ControlEntry | None
    entries: dict[str, _ControlEntry]


def _read_entry(path: Path) -> _ControlEntry | None:
    """Describe *path* without following symlinks; ``None`` if absent or special."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    uid, gid = st.st_uid, st.st_gid
    if stat.S_ISLNK(st.st_mode):
        return _ControlEntry("symlink", data=os.fsencode(os.readlink(path)), uid=uid, gid=gid)
    if stat.S_ISDIR(st.st_mode):
        return _ControlEntry("dir", mode=stat.S_IMODE(st.st_mode), uid=uid, gid=gid)
    if not stat.S_ISREG(st.st_mode):
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read()
    return _ControlEntry("file", mode=stat.S_IMODE(st.st_mode), data=data, uid=uid, gid=gid)


def _read_tree(root: Path, rel: str, entries: dict[str, _ControlEntry]) -> None:
    entry = _read_entry(root / rel)
    if entry is None:
        return
    entries[rel] = entry
    if entry.kind == "dir":
        for child in sorted(os.listdir(root / rel)):
            _read_tree(root, f"{rel}/{child}", entries)


def _read_control(git_dir: Path) -> dict[str, _ControlEntry]:
    entries: dict[str, _ControlEntry] = {}
    for rel in GIT_CONTROL_PATHS:
        _read_tree(git_dir, rel, entries)
    return entries


def _remove(path: Path) -> None:
    """Delete *path*; a symlink is unlinked, never followed.

    Directories are walked with an explicit stack rather than recursion, so an
    agent-built tree of any depth cannot exhaust the interpreter stack, and
    each one is made owner-writable first, so an agent ``chmod`` cannot block
    the removal.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return
    stack = [str(path)]
    while stack:
        top = stack[-1]
        os.chmod(top, 0o700)
        subdirs = []
        with os.scandir(top) as it:
            for child in it:
                if child.is_dir(follow_symlinks=False):
                    subdirs.append(child.path)
                else:
                    os.unlink(child.path)
        if subdirs:
            stack.extend(subdirs)
        else:
            os.rmdir(top)
            stack.pop()


def _discard(path: Path) -> None:
    """Move *path* out of git's reach at once, then delete it.

    The rename is a single operation whatever the tree holds, so once it
    returns no git command sees *path*. A failed delete afterwards is only
    logged: git never reads the renamed copy.
    """
    aside = path.with_name(f"{path.name}.agentic-ci-untrusted-{uuid.uuid4().hex}")
    os.rename(path, aside)
    try:
        _remove(aside)
    except OSError as exc:
        log.warning("Could not delete %s: %s", aside, exc)


def _fail_closed(dot_git: Path, reason: str) -> NoReturn:
    """Take *dot_git* away from host git and raise :class:`GitControlTamperError`."""
    try:
        _discard(dot_git)
    except OSError as exc:
        raise GitControlTamperError(
            f"{reason}; {dot_git} could not be moved aside ({exc}) and still holds the "
            "agent's git config, so host git must not use this repository"
        ) from exc
    raise GitControlTamperError(
        f"{reason}; moved {dot_git} aside so host git cannot use the agent's git config"
    )


def _differs(path: Path, rel: str, expected: dict[str, _ControlEntry]) -> bool:
    """Best effort: whether the tree at *path* differs from the snapshot of *rel*.

    *path* holds agent-written content, so this never raises, never recurses,
    visits no more entries than the snapshot holds and reads no file larger
    than its snapshot copy. Anything it cannot check counts as changed.
    """
    wanted = {k: v for k, v in expected.items() if k == rel or k.startswith(f"{rel}/")}
    seen = 0
    stack = [(str(path), rel)]
    try:
        while stack:
            current, name = stack.pop()
            want = wanted.get(name)
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                if want is not None:
                    return True
                continue
            seen += 1
            if want is None or seen > len(wanted):
                return True
            if stat.S_ISLNK(st.st_mode):
                if want.kind != "symlink" or os.fsencode(os.readlink(current)) != want.data:
                    return True
                continue
            if stat.S_IMODE(st.st_mode) != want.mode:
                return True
            if stat.S_ISDIR(st.st_mode):
                if want.kind != "dir":
                    return True
                with os.scandir(current) as it:
                    for child in it:
                        if len(stack) >= len(wanted):
                            return True
                        stack.append((child.path, f"{name}/{child.name}"))
            elif stat.S_ISREG(st.st_mode):
                if want.kind != "file" or st.st_size != len(want.data):
                    return True
                fd = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as fh:
                    if fh.read(len(want.data) + 1) != want.data:
                        return True
            else:
                return True
        return seen != len(wanted)
    except OSError:
        return True


def _write_entry(path: Path, entry: _ControlEntry) -> None:
    """Create *path* from *entry*; fails if something reappeared in its place."""
    if entry.kind == "dir":
        os.mkdir(path)
        os.chmod(path, entry.mode)
    elif entry.kind == "symlink":
        os.symlink(os.fsdecode(entry.data), path)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, entry.mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(entry.data)
            os.fchmod(fh.fileno(), entry.mode)
    if os.geteuid() == 0 and entry.uid >= 0:
        os.lchown(path, entry.uid, entry.gid)


def snapshot_git_control(repo_dir: Path) -> GitControlSnapshot:
    """Record the git control files of the repository at *repo_dir*.

    Call this on the host before an agent can write *repo_dir* (for example
    before a sandbox upload or a container bind mount), after any hardening
    such as :func:`harden_git_config`. Pass the result to
    :func:`restore_git_control` once the agent can no longer write the
    repository.
    """
    repo_dir = Path(repo_dir)
    dot_git = repo_dir / ".git"
    top = _read_entry(dot_git)
    entries = _read_control(dot_git) if top is not None and top.kind == "dir" else {}
    return GitControlSnapshot(repo_dir=repo_dir, dot_git=top, entries=entries)


def restore_git_control(snapshot: GitControlSnapshot) -> list[str]:
    """Put the host's git control files back after an agent had write access.

    Everything under :data:`GIT_CONTROL_PATHS` is moved out of git's reach and
    rewritten from *snapshot*, so config keys, hooks, attributes and
    ``commondir`` redirects added by the agent are gone before any host git
    command runs, while its commits, refs and index are kept. Nothing the
    agent wrote is read or walked before that, so no tree it built can make
    the restore fail early, and symlinks it planted are never written through.

    A ``.git`` the agent created in a workdir that had none is removed.

    Returns the control paths the agent had changed (best effort, for the
    log). Raises :class:`GitControlTamperError` when ``.git`` was a directory
    and is now missing, a symlink or a file (that entry is deleted first), or
    when the host copy cannot be put back; ``.git`` is then moved aside so
    host git cannot use the agent's config.
    """
    dot_git = snapshot.repo_dir / ".git"
    try:
        current = os.lstat(dot_git)
    except FileNotFoundError:
        current = None
    if snapshot.dot_git is None:
        if current is None:
            return []
        # Host git run from the workdir would use this .git and its config,
        # even when the workdir sits inside another repository.
        try:
            _discard(dot_git)
        except OSError as exc:
            raise GitControlTamperError(
                f"The agent created {dot_git} and it could not be removed ({exc}); "
                "host git must not run in this workdir"
            ) from exc
        log.warning("Removed the .git the agent created in %s", snapshot.repo_dir)
        return [".git"]
    if snapshot.dot_git.kind != "dir":
        # A gitfile or symlink pointing at a git dir outside the workdir,
        # which the agent cannot reach. Put the pointer itself back.
        if not _differs(dot_git, ".git", {".git": snapshot.dot_git}):
            return []
        try:
            if current is not None:
                _discard(dot_git)
            _write_entry(dot_git, snapshot.dot_git)
        except OSError as exc:
            raise GitControlTamperError(
                f"Could not restore {dot_git} ({exc}); host git must not use this repository"
            ) from exc
        return [".git"]
    if current is None or not stat.S_ISDIR(current.st_mode):
        what = "deleted"
        if current is not None:
            kind = "symlink" if stat.S_ISLNK(current.st_mode) else "file"
            what = f"replaced by a {kind}"
            _remove(dot_git)
        raise GitControlTamperError(
            f"{dot_git} was {what} during the agent run; host git must not use this repository"
        )

    # Nothing the agent wrote is read before it is out of git's reach: each
    # control path is renamed into a quarantine directory, which git never
    # reads, and only then compared with the snapshot for the log.
    quarantine = dot_git / f"agentic-ci-untrusted-{uuid.uuid4().hex}"
    try:
        os.chmod(dot_git, snapshot.dot_git.mode)
        os.mkdir(quarantine, 0o700)
        for rel in GIT_CONTROL_PATHS:
            try:
                os.rename(dot_git / rel, quarantine / rel)
            except FileNotFoundError:
                pass
        # Sorted keys create each directory before its children.
        for rel in sorted(snapshot.entries):
            _write_entry(dot_git / rel, snapshot.entries[rel])
    except OSError as exc:
        _fail_closed(dot_git, f"Could not restore the git control files in {dot_git} ({exc})")

    changed = [
        rel for rel in GIT_CONTROL_PATHS if _differs(quarantine / rel, rel, snapshot.entries)
    ]
    try:
        _remove(quarantine)
    except OSError as exc:
        log.warning("Could not delete %s: %s", quarantine, exc)
    if changed:
        log.warning(
            "Agent changed git control files in %s; restored host copy of: %s",
            dot_git,
            ", ".join(changed),
        )
    return changed


def discard_git_dir(repo_dir: Path) -> None:
    """Move ``repo_dir/.git`` aside and delete it.

    For when the host copy cannot be restored safely, for example because an
    agent process may still be running and able to write ``.git``. Host git
    then finds no repository in *repo_dir* instead of the agent's config. The
    agent's commits are lost with it. Failures are logged, not raised, so the
    caller's own error is what propagates.
    """
    dot_git = Path(repo_dir) / ".git"
    if not os.path.lexists(dot_git):
        return
    try:
        _discard(dot_git)
    except OSError as exc:
        log.error("Could not move %s aside (%s); host git must not use it", dot_git, exc)
        return
    log.warning("Moved %s aside: the agent could still write it", dot_git)


def get_commit_info(repo_dir: Path) -> dict:
    """Get the latest commit info (committer, email, message, sha).

    Uses committer identity (not author) so that rebased or
    cherry-picked commits always reflect the current git config.
    """
    fmt = "%H%n%ce%n%cn%n%s"
    result = subprocess.run(
        ["git", "log", "-1", f"--format={fmt}"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.strip().split("\n")
    if len(lines) < 4:
        return {}
    return {"sha": lines[0], "email": lines[1], "name": lines[2], "subject": lines[3]}


class GitDiffError(Exception):
    """Raised when git diff fails (missing ref, not a repo, etc.)."""


def get_changed_files(repo_dir: Path, base_ref: str = "HEAD~1") -> list[str]:
    """Return files changed between *base_ref* and HEAD (committed state only).

    Uses the two-ref form ``git diff --name-only <base_ref> HEAD`` so that
    files still in HEAD after a failed ``git commit --amend`` are detected
    even when ``git rm --cached`` already removed them from the index.

    Raises GitDiffError if the git command fails.
    """
    if not _validate_ref(base_ref):
        raise GitDiffError(f"Invalid ref name: {base_ref}")
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        return [f for f in result.stdout.strip().split("\n") if f]
    except subprocess.CalledProcessError as exc:
        raise GitDiffError(
            f"git diff failed for base_ref={base_ref}: {exc.stderr.strip()}"
        ) from exc


def strip_committed_files(
    repo_dir: Path,
    patterns: list[str],
    base_ref: str = "origin/HEAD",
) -> list[str]:
    """Remove files matching *patterns* from the latest commit.

    Agents can bypass ``.git/info/exclude`` by explicitly naming files in
    ``git add``.  This function detects any committed files that match the
    given fnmatch *patterns* and amends the commit to remove them, keeping
    the working-tree copies intact.

    Returns the list of file paths actually stripped (empty if none matched
    or all removals failed).
    """
    try:
        changed = get_changed_files(repo_dir, base_ref=base_ref)
    except GitDiffError:
        return []

    to_remove = []
    for filepath in changed:
        name = Path(filepath).name
        for pattern in patterns:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(filepath, pattern):
                to_remove.append(filepath)
                break

    if not to_remove:
        return []

    log.warning(
        "Stripping %d artifact file(s) from commit: %s",
        len(to_remove),
        ", ".join(to_remove),
    )
    actually_removed = []
    for filepath in to_remove:
        result = subprocess.run(
            ["git", "rm", "--cached", "--quiet", filepath],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.error(
                "git rm --cached failed for %s (rc=%d): %s",
                filepath,
                result.returncode,
                result.stderr.strip(),
            )
        else:
            actually_removed.append(filepath)

    if actually_removed:
        result = subprocess.run(
            ["git", "commit", "--amend", "--no-edit", "--allow-empty"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.error(
                "git commit --amend failed after stripping (rc=%d): %s",
                result.returncode,
                result.stderr.strip(),
            )

    return actually_removed


# -- Git credential setup ----------------------------------------------------


def setup_git_credentials(
    repo_url: str,
    *,
    github_token_resolver: Callable[[str], str | None] | None = None,
) -> bool:
    """Configure ``git url.insteadOf`` for the forge hosting *repo_url*.

    Sets up transparent credential injection so that ``clone_repo()`` and
    ``push_branch()`` (which use bare HTTPS URLs) can authenticate
    without modification.

    For **GitLab**, reads ``BOT_PAT`` from the environment.
    For **GitHub**, calls *github_token_resolver(repo_url)* to obtain a
    short-lived token. If no resolver is provided for GitHub URLs,
    returns False.

    Idempotent and safe to call multiple times. Returns True on success,
    False if credentials are unavailable.
    """
    if not repo_url:
        return False

    parsed = urlparse(repo_url)
    hostname = (parsed.hostname or "").lower()

    if hostname == "gitlab.com":
        return _setup_gitlab_credentials()
    elif hostname == "github.com":
        if github_token_resolver is None:
            log.error("No github_token_resolver provided for GitHub URL: %s", repo_url)
            return False
        return _setup_github_credentials(repo_url, github_token_resolver)

    log.info("No credential setup needed for URL: %s", repo_url)
    return True


def _setup_gitlab_credentials() -> bool:
    """Configure ``git insteadOf`` for GitLab using ``BOT_PAT`` env var."""
    token = os.environ.get("BOT_PAT")
    if not token:
        log.error("BOT_PAT environment variable not set; cannot authenticate to GitLab")
        return False
    return _set_insteadof(
        f"https://oauth2:{token}@gitlab.com/",
        "https://gitlab.com/",
    )


def _setup_github_credentials(
    repo_url: str,
    token_resolver: Callable[[str], str | None],
) -> bool:
    """Configure ``git insteadOf`` for GitHub using a caller-provided token."""
    token = token_resolver(repo_url)
    if not token:
        log.error("GitHub token resolver returned no token for: %s", repo_url)
        return False
    return _set_insteadof(
        f"https://x-access-token:{token}@github.com/",
        "https://github.com/",
    )


def _set_insteadof(authenticated_prefix: str, original_prefix: str) -> bool:
    """Run ``git config --global url.<auth>.insteadOf <original>``.

    WARNING: Writes credentials to ``~/.gitconfig`` via ``--global``.
    In CI the container is ephemeral so this is safe. For local
    development, credentials persist until manually removed.
    """
    if not os.environ.get("CI"):
        log.warning(
            "Writing git credentials to ~/.gitconfig (--global). "
            "This persists on local machines -- remove manually after use."
        )
    key = f"url.{authenticated_prefix}.insteadOf"
    try:
        subprocess.run(
            ["git", "config", "--global", key, original_prefix],
            check=True,
            capture_output=True,
            text=True,
        )
        log.info("Configured git insteadOf for %s", original_prefix)
        return True
    except subprocess.CalledProcessError as exc:
        log.error("git config --global failed: %s", exc.stderr)
        return False
    except FileNotFoundError:
        log.error("git binary not found")
        return False
