"""Reusable pre- and post-agent gates.

Gates validate data before and after an AI agent runs.  Pre-gates filter
input so the agent never sees invalid data.  Post-gates validate output
to catch dangerous changes before they reach the forge.

All functions are stateless and tracker-agnostic -- they operate on
dicts and file lists, not Jira-specific types.

The ``GATE_REGISTRY`` maps CLI-friendly names to ``GateSpec`` instances.
Use ``resolve_gates()`` to look up gates by name and
``validate_gate_env()`` to check required environment variables before
running any gate.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


# -- Gate registry -----------------------------------------------------------


@dataclass
class GateSpec:
    """Metadata for a registered gate function."""

    name: str
    fn: Callable
    phase: str  # "pre" or "post"
    required_env: list[str] = field(default_factory=list)


GATE_REGISTRY: dict[str, GateSpec] = {}


def _register(name: str, fn: Callable, phase: str, required_env: list[str] | None = None) -> None:
    """Register a gate function in the global registry."""
    GATE_REGISTRY[name] = GateSpec(
        name=name,
        fn=fn,
        phase=phase,
        required_env=required_env or [],
    )


def resolve_gates(names: list[str]) -> list[GateSpec]:
    """Look up gate specs by CLI name. Raises SystemExit on unknown names."""
    specs = []
    unknown = []
    for name in names:
        spec = GATE_REGISTRY.get(name)
        if spec is None:
            unknown.append(name)
        else:
            specs.append(spec)
    if unknown:
        available = ", ".join(sorted(GATE_REGISTRY))
        sys.exit(f"Error: unknown gate(s): {', '.join(unknown)}\nAvailable: {available}")
    return specs


def validate_gate_env(gates: list[GateSpec]) -> None:
    """Check that all required env vars for the given gates are set.

    Fails with a clear error listing every missing variable and
    which gate needs it.
    """
    missing: dict[str, list[str]] = {}
    for gate in gates:
        for var in gate.required_env:
            if not os.environ.get(var):
                missing.setdefault(var, []).append(gate.name)
    if missing:
        lines = [f"  {var} (needed by: {', '.join(names)})" for var, names in missing.items()]
        sys.exit("Error: missing environment variables for gates:\n" + "\n".join(lines))


# -- Post-agent gates -------------------------------------------------------

DEFAULT_SENSITIVE_BLOCKLIST = [
    ".env",
    "credentials.*",
    "*secret*",
    "*.pem",
    "*.key",
    ".git-credentials",
    ".netrc",
]


def check_sensitive_files(
    changed_files: list[str],
    blocklist: list[str] | None = None,
) -> list[str]:
    """Check if any changed files match a sensitive-file blocklist.

    Returns a list of blocked file paths (empty means all clear).
    """
    if blocklist is None:
        blocklist = DEFAULT_SENSITIVE_BLOCKLIST
    blocked = []
    for filepath in changed_files:
        name = Path(filepath).name
        for pattern in blocklist:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(filepath, pattern):
                blocked.append(filepath)
                break
    return blocked


def check_commit_author(commit_info: dict, expected_email: str) -> bool:
    """Verify the commit author matches the expected bot email."""
    actual = commit_info.get("email", "")
    return actual.lower() == expected_email.lower()


def check_commit_message_key(commit_info: dict, ticket_key: str) -> bool:
    """Verify the ticket key appears as a whole token in the commit subject.

    Uses word-boundary matching so "ABC-1" does not match "ABC-10".
    """
    subject = commit_info.get("subject", "")
    escaped = re.escape(ticket_key)
    return bool(re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", subject, re.IGNORECASE))


def log_changed_files(changed_files: list[str], ticket_key: str) -> None:
    """Log changed files for observability."""
    if changed_files:
        log.info(
            "[%s] Agent changed %d files: %s",
            ticket_key,
            len(changed_files),
            ", ".join(changed_files),
        )
    else:
        log.info("[%s] No files changed by agent", ticket_key)


GITLEAKS_TIMEOUT = int(os.environ.get("GITLEAKS_TIMEOUT", "120"))


def gitleaks_scan(repo_dir: Path, compare_ref: str = "origin/HEAD") -> list[str]:
    """Scan new commits for secrets using gitleaks.

    Returns a list of error strings (empty means clean).
    Requires ``gitleaks`` to be installed on PATH.  Fails closed:
    returns an error if gitleaks is missing or times out.
    """
    import shutil

    if not shutil.which("gitleaks"):
        log.error("gitleaks not found on PATH — failing closed")
        return ["gitleaks is not installed; secret scan cannot run"]

    try:
        count_output = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--count", f"{compare_ref}..HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        log.error("git rev-list failed: %s", exc.stderr.strip())
        return [f"gitleaks pre-check failed: git rev-list error: {exc.stderr.strip()}"]

    try:
        commit_count = int(count_output.stdout.strip())
    except ValueError:
        log.error("git rev-list returned non-integer: %r", count_output.stdout.strip())
        return ["gitleaks pre-check failed: could not parse commit count"]

    if commit_count == 0:
        log.info("gitleaks scan skipped: no commits in range %s..HEAD", compare_ref)
        return []

    try:
        result = subprocess.run(
            [
                "gitleaks",
                "detect",
                "--source",
                str(repo_dir),
                f"--log-opts={compare_ref}..HEAD",
                "--verbose",
            ],
            capture_output=True,
            text=True,
            timeout=GITLEAKS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.error("gitleaks timed out after %ds", GITLEAKS_TIMEOUT)
        return [f"gitleaks timed out after {GITLEAKS_TIMEOUT}s; secret scan inconclusive"]

    if result.returncode != 0:
        log.error("gitleaks detected secrets in committed changes")
        return [
            "gitleaks detected potential secrets in committed code. "
            "Review the gitleaks output in the CI job log for details."
        ]

    log.info("gitleaks scan passed: no secrets found")
    return []


# -- Pre-agent gates ---------------------------------------------------------


def filter_comments_by_domain(
    comments: list[dict],
    allowed_domain_re: re.Pattern[str],
) -> list[dict]:
    """Keep only comments from authors whose email matches ``allowed_domain_re``."""
    return [c for c in comments if allowed_domain_re.search(c.get("author_email", ""))]


def filter_bot_comments(
    comments: list[dict],
    sentinel_phrases: list[str],
) -> list[dict]:
    """Remove comments containing any of the given bot sentinel phrases."""
    return [c for c in comments if not any(s in c.get("body", "") for s in sentinel_phrases)]


def check_external_reporter(
    ticket: dict,
    internal_domain_re: re.Pattern[str],
    *,
    external_label: str,
) -> str | None:
    """Check if a ticket was filed by an external reporter.

    Returns ``external_label`` if the reporter is external and the label
    is not already present, otherwise ``None``.
    """
    reporter_email = ticket.get("reporter_email", "")
    labels = ticket.get("labels", [])
    if external_label in labels:
        return None
    if not internal_domain_re.search(reporter_email):
        return external_label
    return None


# -- CLI gate runners --------------------------------------------------------
# These wrap the core gate functions with workdir-derived context
# so the CLI can invoke them by name.


def _run_sensitive_files(workdir: str, **_kw: object) -> list[str]:
    """CLI runner for the sensitive-files gate."""
    from agentic_ci.git import GitDiffError, get_changed_files

    try:
        changed = get_changed_files(Path(workdir), base_ref="origin/HEAD")
    except GitDiffError as exc:
        return [f"Could not compute changed files: {exc}"]

    blocked = check_sensitive_files(changed)
    if blocked:
        return [f"Sensitive files modified: {', '.join(blocked)}"]
    return []


def _run_commit_author(workdir: str, **_kw: object) -> list[str]:
    """CLI runner for the commit-author gate."""
    from agentic_ci.git import get_commit_info

    expected = os.environ.get("BOT_EMAIL", "")
    try:
        info = get_commit_info(Path(workdir))
    except subprocess.CalledProcessError as exc:
        return [f"Could not read commit info: {exc}"]

    if not check_commit_author(info, expected):
        return [f"Commit author '{info.get('email')}' does not match expected '{expected}'"]
    return []


def _run_commit_message_key(workdir: str, **_kw: object) -> list[str]:
    """CLI runner for the commit-message-key gate."""
    from agentic_ci.git import get_commit_info

    ticket_key = os.environ.get("TICKET_KEY", "")
    try:
        info = get_commit_info(Path(workdir))
    except subprocess.CalledProcessError as exc:
        return [f"Could not read commit info: {exc}"]

    if not check_commit_message_key(info, ticket_key):
        return [f"Ticket key '{ticket_key}' not found in commit message: '{info.get('subject')}'"]
    return []


def _run_gitleaks(workdir: str, **_kw: object) -> list[str]:
    """CLI runner for the gitleaks gate."""
    return gitleaks_scan(Path(workdir))


# -- Register built-in gates -------------------------------------------------

_register("sensitive-files", _run_sensitive_files, phase="post")
_register("commit-author", _run_commit_author, phase="post", required_env=["BOT_EMAIL"])
_register(
    "commit-message-key",
    _run_commit_message_key,
    phase="post",
    required_env=["TICKET_KEY"],
)
_register("gitleaks", _run_gitleaks, phase="post")
