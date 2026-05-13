"""Podman-based container executor for running Claude Code.

Replaces the bash scripts (claude-runner.sh, run_container.sh) with a
single Python module. Builds a ``podman run`` command that launches the
CI container image with ``agentic-ci run`` as the entrypoint.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from agentic_ci.credentials import stage_credentials_for_mount

log = logging.getLogger(__name__)

_DEFAULT_IMAGE = "ghcr.io/opendatahub-io/ai-helpers:latest"
_DEFAULT_TIMEOUT = 1200
_DEFAULT_MODEL = "claude-opus-4-6"
_DEFAULT_PROJECT = "itpc-gcp-ai-eng-claude"
_DEFAULT_REGION = "global"


def run_container(
    image: str | None,
    workdir: str | Path,
    prompt: str,
    *,
    model: str | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    credentials_dir: str | Path | None = None,
    extra_podman_args: list[str] | None = None,
    extra_claude_args: list[str] | None = None,
    streaming: bool = True,
    output_file: str | Path | None = None,
) -> int:
    """Launch Claude in a Podman container.

    The container runs ``agentic-ci run`` as its command, which handles
    OTEL collection, Claude invocation, and stream processing internally.

    Returns the container exit code.
    """
    image = image or os.environ.get("CLAUDE_CONTAINER_IMAGE", _DEFAULT_IMAGE)
    model = model or os.environ.get("CLAUDE_MODEL", _DEFAULT_MODEL)
    timeout = timeout or int(os.environ.get("CLAUDE_CONTAINER_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID",
                             os.environ.get("GCP_PROJECT_ID", _DEFAULT_PROJECT))
    region = os.environ.get("CLOUD_ML_REGION", _DEFAULT_REGION)

    workdir = Path(workdir).resolve()

    if os.getuid() == 0:
        try:
            for root, dirs, files in os.walk(workdir):
                os.chown(root, 1000, 1000)
                for name in dirs + files:
                    os.chown(os.path.join(root, name), 1000, 1000)
        except OSError as exc:
            log.debug("chown failed (non-fatal): %s", exc)

    # Stage credentials if not provided
    cleanup_creds = False
    if credentials_dir is None:
        credentials_dir = Path(tempfile.mkdtemp(prefix="agentic-ci-creds-"))
        cleanup_creds = True
        try:
            stage_credentials_for_mount(credentials_dir)
        except RuntimeError:
            log.warning("No GCP credentials found; container may fail auth")

    creds_path = Path(credentials_dir)
    adc_file = creds_path / ".config/gcloud/application_default_credentials.json"
    cfg_file = creds_path / ".config/gcloud/configurations/config_default"

    # Build command
    cmd: list[str] = [
        "podman", "run", "--rm",
        "--pull", "newer",
        "--userns=keep-id:uid=1000,gid=1000",
        "--timeout", str(timeout),
        "--workdir", "/workspace",
    ]

    # Volume mounts
    cmd.extend(["-v", f"{workdir}:/workspace:z"])
    if adc_file.exists():
        cmd.extend([
            "-v",
            f"{adc_file}:/home/claude/.config/gcloud/application_default_credentials.json:ro,z",
        ])
    if cfg_file.exists():
        cmd.extend([
            "-v", f"{cfg_file}:/home/claude/.config/gcloud/configurations/config_default:ro,z",
        ])

    prompt_fd, prompt_path = tempfile.mkstemp(prefix="agentic-ci-prompt-", suffix=".txt")
    prompt_tmp = Path(prompt_path)
    with os.fdopen(prompt_fd, "w", encoding="utf-8") as f:
        f.write(prompt)
    prompt_tmp.chmod(0o644)
    cmd.extend(["-v", f"{prompt_tmp}:/tmp/.claude-prompt.txt:ro,z"])

    # Environment variables
    env_vars = {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLOUD_ML_REGION": region,
        "ANTHROPIC_VERTEX_PROJECT_ID": project,
        "DISABLE_AUTOUPDATER": "1",
    }
    if env:
        env_vars.update(env)

    for key, val in env_vars.items():
        cmd.extend(["--env", f"{key}={val}"])

    if extra_podman_args:
        cmd.extend(extra_podman_args)

    # Image + in-container command
    cmd.append(image)

    # The container runs agentic-ci to handle OTEL + streaming + Claude
    cmd.extend([
        "agentic-ci", "run",
        "--model", model,
    ])
    if not streaming:
        cmd.append("--no-streaming")

    if extra_claude_args:
        cmd.extend(["--"] + extra_claude_args)

    # The prompt is read from the mounted file
    cmd.append("Read and follow all instructions in /tmp/.claude-prompt.txt exactly.")

    log.info("Launching container: %s (model=%s, timeout=%ds)", image, model, timeout)

    try:
        result = subprocess.run(cmd, capture_output=False)
        rc = result.returncode

        # Move container output log if present
        container_output = workdir / ".claude-output.txt"
        if output_file and container_output.exists():
            shutil.move(str(container_output), str(output_file))

        return rc
    finally:
        prompt_tmp.unlink(missing_ok=True)
        if cleanup_creds:
            shutil.rmtree(creds_path, ignore_errors=True)
