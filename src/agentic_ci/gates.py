"""Reusable pre- and post-agent gates.

Gates validate data before and after an AI agent runs.  Pre-gates filter
input so the agent never sees invalid data.  Post-gates validate output
to catch dangerous changes before they reach the forge.

All functions are stateless and tracker-agnostic -- they operate on
dicts and file lists, not Jira-specific types.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

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
    """Verify the ticket key appears in the commit message subject."""
    subject = commit_info.get("subject", "")
    return ticket_key.upper() in subject.upper()


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


def gitleaks_scan(repo_dir: Path, compare_ref: str = "origin/HEAD") -> list[str]:
    """Scan new commits for secrets using gitleaks.

    Returns a list of error strings (empty means clean).
    Requires ``gitleaks`` to be installed on PATH.
    """
    import shutil

    if not shutil.which("gitleaks"):
        log.warning("gitleaks not found on PATH, skipping secret scan")
        return []

    try:
        count_output = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--count", f"{compare_ref}..HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        commit_count = int(count_output.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        commit_count = 0

    if commit_count == 0:
        log.info("gitleaks scan skipped: no commits in range %s..HEAD", compare_ref)
        return []

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
    )

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
) -> list[str]:
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
