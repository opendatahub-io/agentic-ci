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


class AcliError(Exception):
    """Raised when an acli command fails."""

    def __init__(self, message: str, returncode: int = 1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def is_available() -> bool:
    """Check if acli is on PATH."""
    return shutil.which("acli") is not None


def ensure_acli(dest: str = "/usr/local/bin/acli") -> str:
    """Download acli if not already on PATH. Returns path to binary."""
    if is_available():
        return shutil.which("acli")  # type: ignore[return-value]

    log.info("Downloading acli to %s", dest)
    try:
        subprocess.run(
            ["curl", "-fsSL", ACLI_DOWNLOAD_URL, "-o", dest],
            check=True,
            capture_output=True,
        )
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
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

    result = subprocess.run(
        ["acli", "jira", "auth", "login", "--email", email, "--site", site, "--token"],
        input=token,
        capture_output=True,
        text=True,
    )
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
    cmd = ["acli", *args]
    if json_output:
        cmd.append("--json")

    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if check and result.returncode != 0:
        raise AcliError(
            f"acli command failed: {' '.join(args)}\n{result.stderr}",
            returncode=result.returncode,
            stderr=result.stderr,
        )

    return result
