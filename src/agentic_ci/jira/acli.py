"""Atlassian CLI (acli) wrapper for agentic-ci.

Downloads the acli binary if not already on PATH, handles
authentication, and provides a subprocess runner for acli commands.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess

log = logging.getLogger(__name__)

ACLI_DOWNLOAD_URL = "https://acli.atlassian.com/linux/latest/acli_linux_amd64/acli"
DEFAULT_SITE = "redhat.atlassian.net"
SUBPROCESS_TIMEOUT = int(os.environ.get("ACLI_TIMEOUT", "30"))

_SENSITIVE_FLAGS = frozenset({"--token", "--password", "--api-token"})


class AcliError(Exception):
    """Raised when an acli command fails."""

    def __init__(self, message: str, returncode: int = 1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def _redact_cmd(cmd: list[str]) -> list[str]:
    """Return a copy of *cmd* with sensitive flag values replaced."""
    redacted: list[str] = []
    skip_next = False
    for arg in cmd:
        if skip_next:
            redacted.append("REDACTED")
            skip_next = False
            continue
        if "=" in arg:
            flag, _, _ = arg.partition("=")
            if flag in _SENSITIVE_FLAGS:
                redacted.append(f"{flag}=REDACTED")
                continue
        if arg in _SENSITIVE_FLAGS:
            redacted.append(arg)
            skip_next = True
            continue
        redacted.append(arg)
    return redacted


def _resolve_acli() -> str | None:
    """Return the absolute path to acli, or None if not on PATH."""
    return shutil.which("acli")


def is_available() -> bool:
    """Check if acli is on PATH."""
    return _resolve_acli() is not None


def ensure_acli(dest: str = "/usr/local/bin/acli") -> str:
    """Download acli if not already on PATH. Returns absolute path to binary."""
    existing = _resolve_acli()
    if existing:
        return existing

    curl_path = shutil.which("curl")
    if not curl_path:
        raise AcliError("curl not found on PATH; cannot download acli")

    log.info("Downloading acli to %s", dest)
    try:
        subprocess.run(
            [curl_path, "-fsSL", ACLI_DOWNLOAD_URL, "-o", dest],
            check=True,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except subprocess.TimeoutExpired as exc:
        raise AcliError(f"acli download timed out after {SUBPROCESS_TIMEOUT}s") from exc
    except subprocess.CalledProcessError as exc:
        raise AcliError(
            f"Failed to download acli: {exc.stderr.decode()}", returncode=exc.returncode
        ) from exc

    return dest


def setup_auth(site: str = DEFAULT_SITE) -> None:
    """Authenticate acli using JIRA_EMAIL + JIRA_API_TOKEN env vars."""
    email = os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USER", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not email or not token:
        missing = []
        if not email:
            missing.append("JIRA_EMAIL (or JIRA_USER)")
        if not token:
            missing.append("JIRA_API_TOKEN")
        raise AcliError(f"Missing env vars for acli auth: {', '.join(missing)}")

    acli_path = _resolve_acli()
    if not acli_path:
        raise AcliError("acli not found on PATH; call ensure_acli() first")

    try:
        result = subprocess.run(
            [acli_path, "jira", "auth", "login", "--email", email, "--site", site, "--token"],
            input=token,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise AcliError(f"acli auth timed out after {SUBPROCESS_TIMEOUT}s") from exc

    if result.returncode != 0:
        raise AcliError(
            f"acli auth failed: {result.stderr}",
            returncode=result.returncode,
            stderr=result.stderr,
        )
    log.info("acli authenticated against %s", site)


def run_acli(
    *args: str,
    json_output: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run an acli command.

    If ``json_output`` is True, appends ``--json`` to the command.
    If ``check`` is True, raises ``AcliError`` on non-zero exit.
    """
    acli_path = _resolve_acli()
    if not acli_path:
        raise AcliError("acli not found on PATH")

    cmd = [acli_path, *args]
    if json_output:
        cmd.append("--json")

    log.debug("Running: %s", " ".join(_redact_cmd(cmd)))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise AcliError(
            f"acli command timed out after {SUBPROCESS_TIMEOUT}s: {' '.join(_redact_cmd(cmd))}"
        ) from exc

    if check and result.returncode != 0:
        raise AcliError(
            f"acli command failed: {' '.join(_redact_cmd(list(args)))}\n{result.stderr}",
            returncode=result.returncode,
            stderr=result.stderr,
        )

    return result
