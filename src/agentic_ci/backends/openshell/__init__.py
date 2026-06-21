"""OpenShell sandbox backend for agentic-ci."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import threading
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


_OPENSHELL_HOST = "host.openshell.internal"


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
        *,
        harness: Harness,
    ):
        super().__init__(workdir=workdir, image=image, harness=harness)
        self.policy_path = policy
        self._extra_env = extra_env or {}
        self.approval_mode = approval_mode

    def setup(self, otel_port=None):
        if not gateway.is_running():
            log.section("Starting OpenShell gateway")
            gateway.start()
        else:
            log.section("OpenShell gateway already running")

        log.section("Configuring provider")
        provider.setup(auth_mode=self.harness.auth_mode)

        if sandbox.exists():
            log.section("Sandbox already exists")
            return

        image_info = f", image: {self.image}" if self.image else ""
        log.section(f"Creating sandbox ({image_info.lstrip(', ') or 'default image'})")

        sandbox.create(
            image=self.image,
            policy_path=self.policy_path,
            otel_port=otel_port,
            workdir=self.workdir,
            approval_mode=self.approval_mode,
        )

        log.section("Uploading workdir")
        sandbox.upload(self.workdir)

    def stop(self):
        try:
            if gateway.is_running() and sandbox.exists():
                sandbox.delete()
                log.section("Sandbox deleted")
            else:
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
    ):
        self._write_env_script(model, otel_port, otel_rate_file)
        agent_args = self.harness.build_args(prompt, model, extra_args)

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
        if self.harness.auth_mode == "vertex":
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

    def _write_env_script(self, model, otel_port=None, otel_rate_file=None):
        """Write env vars to a script inside the sandbox, sourced before the agent runs.

        Uses the harness's native env script (Vertex AI vars or API key)
        since the google-cloud provider injects GCP credentials directly.

        For OTEL, uses ``host.openshell.internal`` to reach the host-side
        collector through the gateway proxy instead of the harness default
        (which uses an IP unreachable from the sandbox).
        """
        lines = self.harness.build_env_script_lines()
        if otel_port:
            lines.extend(
                [
                    "export CLAUDE_CODE_ENABLE_TELEMETRY=1",
                    "export OTEL_METRICS_EXPORTER=otlp",
                    "export OTEL_LOGS_EXPORTER=otlp",
                    "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
                    f"export OTEL_EXPORTER_OTLP_ENDPOINT=http://{_OPENSHELL_HOST}:{otel_port}",
                    "export OTEL_METRIC_EXPORT_INTERVAL=5000",
                ]
            )
        else:
            lines.append("export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")

        for key, val in self._extra_env.items():
            lines.append(f"export {key}={shlex.quote(val)}")

        lines.append(f"export AGENT_MODEL={shlex.quote(model)}")

        lines.append("agentic-ci enable-plugins")

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
