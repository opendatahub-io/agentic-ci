"""OpenShell sandbox backend for agentic-ci."""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from typing import TYPE_CHECKING

from agentic_ci import log
from agentic_ci.backend import Backend
from agentic_ci.backends.openshell import gateway, provider, sandbox

if TYPE_CHECKING:
    from agentic_ci.harness import Harness

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

        self._upload_sandbox_config(otel_enabled=otel_port is not None)

    def _upload_sandbox_config(self, otel_enabled=False):
        """Write harness-specific config and upload it to the sandbox."""
        config_dir = tempfile.mkdtemp(prefix="agentic-ci-config-")
        self.harness.write_sandbox_config(config_dir, otel_enabled=otel_enabled)
        for host_path, container_path in self.harness.sandbox_config_mounts(config_dir):
            sandbox.upload(host_path)
            fname = os.path.basename(host_path)
            cmd = f"mkdir -p $(dirname {container_path}) && mv {fname} {container_path}"
            sandbox.exec_cmd(["bash", "-c", cmd])
        shutil.rmtree(config_dir, ignore_errors=True)

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
        proc = sandbox.exec_cmd_streaming(cmd)

        rc = self._process_stream(proc, streaming)
        self._wait_for_otel_flush(otel_port)

        log.section("Downloading workdir")
        sandbox.download(sandbox_workdir, self.workdir)

        return rc

    def _write_env_script(self, model, otel_port=None, otel_rate_file=None):
        """Write env vars to a script inside the sandbox, sourced before the agent runs.

        Uses the harness's native env script (Vertex AI vars, API key, and
        OTEL vars) since the google-cloud provider injects GCP credentials
        directly. The harness handles OTEL endpoint configuration using the
        gateway host address.
        """
        lines = self.harness.build_env_script_lines(otel_port=otel_port)
        if not otel_port and self.harness.name == "Claude Code":
            lines.append("export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")

        for key, val in self._extra_env.items():
            lines.append(f"export {key}={shlex.quote(val)}")

        lines.append(f"export AGENT_MODEL={shlex.quote(model)}")

        lines.extend(
            [
                "if [ -f /usr/local/bin/entrypoint.sh ]; then",
                "    . /usr/local/bin/entrypoint.sh --source-only",
                "    _enable_plugins",
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
