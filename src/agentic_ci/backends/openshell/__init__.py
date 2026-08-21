"""OpenShell sandbox backend for agentic-ci."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_ci import log
from agentic_ci.backend import Backend
from agentic_ci.backends.openshell import gateway, provider, sandbox

if TYPE_CHECKING:
    from agentic_ci.harness import Harness

# GCP access tokens minted by the OpenShell gateway live for 3600s. The
# gateway's refresh worker is supposed to rotate them ahead of expiry, but
# around the hourly boundary a transient mint failure (retried only every 60s)
# can let the token lapse, producing a burst of 401s that exhausts the agent's
# retry budget and kills the run mid-way (see NVIDIA/OpenShell PR #1763).
#
# Force a rotation well inside the token lifetime so a freshly minted token is
# always present, tolerating a couple of failed rotations without draining the
# token's remaining life.
_TOKEN_KEEPALIVE_INTERVAL = 1200  # rotate every 20 min

# Phase-offset the first rotation by 10 min so the 20-min cadence lands at
# 10/30/50/70/... min, never coinciding with the ~hourly expiry boundary that
# the gateway refresh worker and the agent's client token cache already act on.
# Rotating on top of that natural re-fetch correlated with extra transient
# errors; offsetting avoids the collision.
_TOKEN_KEEPALIVE_OFFSET = 600  # 10 min


def _token_keepalive(stop: threading.Event) -> None:
    """Force-rotate the gateway's GCP access token on a phase-offset 20-min
    cadence until *stop* is set. Failures are logged but never raised."""
    if stop.wait(_TOKEN_KEEPALIVE_OFFSET):
        return
    while True:
        try:
            provider.rotate_token()
        except subprocess.CalledProcessError as exc:
            print(
                f"  [token-keepalive] rotate failed (rc={exc.returncode}): "
                f"{exc.stderr.strip() if exc.stderr else ''}",
                flush=True,
            )
        if stop.wait(_TOKEN_KEEPALIVE_INTERVAL):
            return


# Claude Code's API retry budget (the "Retry N/10" counter in the stream).
# The default is 10, but a Vertex token-rotation lapse can produce a burst of
# retryable "unknown" errors that, on stock 60-min token intervals, exhausted
# all 10 retries and killed the run. The 20-min token keepalive shortens those
# windows; this widens the budget so even an unlucky long lapse recovers.
# Belt-and-suspenders with the keepalive above. Overridable via env var.
_DEFAULT_MAX_RETRIES = "20"

_OPENSHELL_HOST = "host.openshell.internal"
_OPENAI_CREDENTIAL_ENV_VARS = frozenset({"OPENAI_API_KEY"})
_OPENSHELL_STATE_ENV = "AGENTIC_CI_OPENSHELL_STATE"
_DEFAULT_OPENSHELL_STATE = Path.home() / ".config" / "agentic-ci" / "openshell-sandbox.json"


def _openshell_state_path() -> Path:
    return Path(os.environ.get(_OPENSHELL_STATE_ENV, _DEFAULT_OPENSHELL_STATE))


def _load_sandbox_identity() -> dict | None:
    try:
        data = json.loads(_openshell_state_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_sandbox_identity(identity: dict) -> None:
    state_path = _openshell_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8")


def _clear_sandbox_identity() -> None:
    try:
        _openshell_state_path().unlink()
    except FileNotFoundError:
        pass


def _sandbox_identity(harness_name: str, image: str | None, auth_mode: str) -> dict:
    return {"auth_mode": auth_mode, "harness": harness_name, "image": image}


class OpenShellBackend(Backend):
    """Runs an AI agent inside an OpenShell sandbox.

    OpenShell provides security-focused sandboxing with network policy
    enforcement, filesystem isolation, and Landlock-based access control.
    Authentication is handled through the OpenShell google-cloud provider,
    which injects GCP credentials via the supervisor proxy. The agent
    uses its native Vertex AI integration directly.

    Unlike PodmanBackend, which bind-mounts the workdir so changes are
    visible immediately on the host, OpenShellBackend copies the workdir
    into the sandbox on setup() and copies it back after run() completes.
    Only changes inside the workdir are reflected back to the host; files
    written elsewhere in the sandbox (e.g. /tmp) are not retrieved.
    """

    collector_bind_address = "0.0.0.0"
    _ENV_SCRIPT = "/tmp/.agentic-ci-env.sh"

    def __init__(
        self,
        workdir=".",
        image=None,
        policy=None,
        extra_env=None,
        approval_mode=None,
        memory=None,
        cpu=None,
        gpu=None,
        *,
        harness: Harness,
    ):
        super().__init__(workdir=workdir, image=image, harness=harness)
        self.policy_path = policy
        self._extra_env = extra_env or {}
        self.approval_mode = approval_mode
        self.memory = memory
        self.cpu = cpu
        self.gpu = gpu

    def _merged_env(self):
        return {**os.environ, **self._extra_env}

    def setup(self, otel_port=None):
        env = self._merged_env()
        auth_mode = self.harness.auth_mode_for_env(env)
        provider.validate_credentials(auth_mode, env)

        if not gateway.is_running():
            log.section("Starting OpenShell gateway")
            gateway.start()
        else:
            log.section("OpenShell gateway already running")

        sandbox_exists = sandbox.exists()
        existing_provider = provider.provider_exists()
        existing_auth_mode = None
        if existing_provider:
            existing_auth_mode = provider.auth_mode()
            if existing_auth_mode is None:
                raise RuntimeError(
                    "Could not determine the existing OpenShell provider auth mode; "
                    "run agentic-ci stop before switching harnesses"
                )

        if sandbox_exists:
            if not existing_provider:
                raise RuntimeError(
                    "The existing OpenShell sandbox has no identifiable provider; "
                    "run agentic-ci stop before switching harnesses"
                )
            identity = _load_sandbox_identity()
            expected_identity = _sandbox_identity(self.harness.name, self.image, auth_mode)
            if identity is None:
                raise RuntimeError(
                    "Could not determine the existing OpenShell sandbox identity; "
                    "run agentic-ci stop before switching harnesses"
                )
            if existing_auth_mode == auth_mode and identity == expected_identity:
                log.section("Sandbox already exists")
                return

            if existing_auth_mode != auth_mode:
                log.section("Auth mode changed; recreating OpenShell sandbox and provider")
            else:
                log.section("Sandbox identity changed; recreating OpenShell sandbox")
            sandbox.delete()
            _clear_sandbox_identity()

        if existing_provider and existing_auth_mode != auth_mode:
            provider.delete()

        log.section("Configuring provider")
        provider.setup(auth_mode=auth_mode, env=env)

        image_info = f", image: {self.image}" if self.image else ""
        log.section(f"Creating sandbox ({image_info.lstrip(', ') or 'default image'})")

        sandbox.create(
            image=self.image,
            policy_path=self.policy_path,
            otel_port=otel_port,
            workdir=self.workdir,
            approval_mode=self.approval_mode,
            auth_mode=auth_mode,
            memory=self.memory,
            cpu=self.cpu,
            gpu=self.gpu,
        )

        self._run_setup_steps()

        log.section("Uploading workdir")
        sandbox.upload(self.workdir)

        self._upload_sandbox_config(otel_enabled=otel_port is not None)
        _save_sandbox_identity(_sandbox_identity(self.harness.name, self.image, auth_mode))

    def _upload_sandbox_config(self, otel_enabled=False):
        """Write harness-specific config and upload it to the sandbox."""
        config_dir = tempfile.mkdtemp(prefix="agentic-ci-config-")
        try:
            self.harness.write_sandbox_config(config_dir, otel_enabled=otel_enabled)
            for host_path, container_path in self.harness.sandbox_config_mounts(config_dir):
                sandbox.upload(host_path)
                fname = os.path.basename(host_path)
                target_dir = os.path.dirname(container_path)
                sandbox.exec_cmd(["mkdir", "-p", target_dir])
                sandbox.exec_cmd(["mv", fname, container_path])
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

    def stop(self):
        try:
            if gateway.is_running() and sandbox.exists():
                sandbox.delete()
                _clear_sandbox_identity()
                log.section("Sandbox deleted")
            else:
                _clear_sandbox_identity()
                log.section("No sandbox to stop")
        finally:
            gateway.stop()
            log.section("Gateway stopped")

    def run(
        self,
        prompt,
        model,
        streaming=True,
        otel_port=None,
        otel_rate_file=None,
        extra_args=None,
        traceparent=None,
    ):
        env = self._merged_env()
        auth_mode = self.harness.auth_mode_for_env(env)
        self._write_env_script(
            model,
            otel_port,
            otel_rate_file,
            traceparent=traceparent,
            env=env,
            auth_mode=auth_mode,
        )
        otel_endpoint = f"http://{_OPENSHELL_HOST}:{otel_port}" if otel_port else None
        agent_args = self.harness.build_args(prompt, model, extra_args, otel_endpoint=otel_endpoint)

        workdir_name = os.path.basename(self.workdir)
        sandbox_workdir = f"/sandbox/{workdir_name}"
        cmd = [
            "bash",
            "-c",
            f'cd {shlex.quote(sandbox_workdir)} && . {self._ENV_SCRIPT} && exec "$@"',
            "--",
            *agent_args,
        ]

        stop_keepalive = threading.Event()
        keepalive: threading.Thread | None = None

        # The token-lapse race only affects the OpenShell gateway's minted
        # Vertex credential; the API-key auth path is unaffected.
        if auth_mode == "vertex":
            log.section("Starting GCP token keepalive")
            keepalive = threading.Thread(
                target=_token_keepalive, args=(stop_keepalive,), daemon=True
            )
            keepalive.start()

        try:
            proc = sandbox.exec_cmd_streaming(cmd)

            rc, stream_complete = self._process_stream(proc, streaming)
            self._wait_for_otel_flush(otel_port)

            log.section("Downloading workdir")
            sandbox.download(sandbox_workdir, self.workdir)

            rc = self._resolve_exit_code(rc, stream_complete)
            return rc
        finally:
            stop_keepalive.set()
            if keepalive:
                keepalive.join(timeout=5)

    def _write_env_script(
        self,
        model,
        otel_port=None,
        otel_rate_file=None,
        traceparent=None,
        env=None,
        auth_mode=None,
    ):
        """Write env vars to a script inside the sandbox, sourced before the agent runs.

        Uses the harness's native env script (Vertex AI vars, API key, and
        OTEL vars) since the google-cloud provider injects GCP credentials
        directly. The harness handles OTEL endpoint configuration using the
        gateway host address.
        """
        env = self._merged_env() if env is None else env
        auth_mode = self.harness.auth_mode_for_env(env) if auth_mode is None else auth_mode
        lines = self.harness.build_env_script_lines(
            otel_port=otel_port,
            traceparent=traceparent,
            env=env,
        )
        if otel_port:
            # The harness sets the OTel endpoint to 10.200.0.1 (the gateway IP
            # used by the Podman backend). OpenShell sandboxes can't reach that
            # address — they resolve the host via host.openshell.internal.
            lines.append(f"export OTEL_EXPORTER_OTLP_ENDPOINT=http://{_OPENSHELL_HOST}:{otel_port}")
        if not otel_port and self.harness.name == "Claude Code":
            lines.append("export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")

        if auth_mode == "vertex":
            max_retries = env.get("CLAUDE_CODE_MAX_RETRIES", _DEFAULT_MAX_RETRIES)
            lines.append(f"export CLAUDE_CODE_MAX_RETRIES={shlex.quote(max_retries)}")

        for key, val in self._extra_env.items():
            if auth_mode == "openai" and key in _OPENAI_CREDENTIAL_ENV_VARS:
                continue
            lines.append(f"export {key}={shlex.quote(val)}")

        lines.append(f"export AGENT_MODEL={shlex.quote(model)}")

        lines.extend(
            [
                "if command -v agentic-ci >/dev/null 2>&1; then",
                "    agentic-ci enable-plugins",
                "fi",
            ]
        )

        script = "\n".join(lines) + "\n"

        with tempfile.NamedTemporaryFile(
            mode="w", prefix="agentic-ci-env-", suffix=".sh", delete=False
        ) as f:
            f.write(script)
            local_path = f.name

        sandbox.upload(local_path)
        sandbox.exec_cmd(
            ["bash", "-c", f"mv {shlex.quote(os.path.basename(local_path))} {self._ENV_SCRIPT}"]
        )
        os.unlink(local_path)
