"""Generic git operations for CI pipelines.

Host-side git operations: clone, push, branch creation, diff inspection.
All operations use subprocess calls to git.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote as urlquote
from urllib.parse import urlparse
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset({"github.com", "gitlab.com"})
GIT_CLONE_TIMEOUT = int(os.environ.get("GIT_CLONE_TIMEOUT", "300"))
GIT_PUSH_TIMEOUT = int(os.environ.get("GIT_PUSH_TIMEOUT", "120"))


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
        if url in seen:
            continue
        if _SUBPATH_RE.search(url):
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


_SAFE_REMOTE_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def push_branch(repo_dir: Path, remote: str = "origin", branch: str | None = None) -> bool:
    """Push the current branch to remote. Returns True on success."""
    if not remote or remote.startswith("-") or ".." in remote or "@{" in remote:
        log.error("push_branch: invalid remote name: %s", remote)
        return False
    if not _SAFE_REMOTE_RE.match(remote):
        log.error("push_branch: invalid remote name: %s", remote)
        return False
    if branch and not _validate_ref(branch):
        log.error("push_branch: invalid branch name: %s", branch)
        return False
    cmd = ["git", "-c", "safe.directory=*", "push", "--set-upstream", remote]
    if branch:
        cmd.append(branch)
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
        return True
    except subprocess.TimeoutExpired:
        log.error("git push timed out after %ds", GIT_PUSH_TIMEOUT)
        return False
    except subprocess.CalledProcessError as exc:
        log.error("git push failed: %s", exc.stderr)
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


def get_commit_info(repo_dir: Path) -> dict:
    """Get the latest commit info (author, email, message, sha)."""
    fmt = "%H%n%ae%n%an%n%s"
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "log", "-1", f"--format={fmt}"],
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
    """Get list of files changed relative to base_ref.

    Raises GitDiffError if the git command fails.
    """
    if not _validate_ref(base_ref):
        raise GitDiffError(f"Invalid ref name: {base_ref}")
    try:
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "diff", "--name-only", base_ref],
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
