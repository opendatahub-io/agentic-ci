"""OpenShell sandbox backend for agentic-ci."""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import TYPE_CHECKING

from agentic_ci import log
from agentic_ci.backend import Backend
from agentic_ci.backends.openshell import gateway, policy, sandbox

if TYPE_CHECKING:
    from agentic_ci.harness import Harness


class OpenShellBackend(Backend):
    """Runs an AI agent inside an OpenShell sandbox.

    OpenShell provides security-focused sandboxing with network policy
    enforcement, filesystem isolation, and Landlock-based access control.
    """

    _ENV_SCRIPT = "/tmp/.agentic-ci-env.sh"

    def __init__(self, workdir=".", image=None, policy=None, *, harness: Harness):
        super().__init__(workdir=workdir, image=image, harness=harness)
        self.policy_path = policy

    def setup(self):
        if not gateway.is_running():
            log.section("Starting OpenShell gateway")
            gateway.start()
        else:
            log.section("OpenShell gateway already running")

        if sandbox.exists():
            log.section("Sandbox already exists")
            return

        resolved_policy = policy.resolve(
            flag_path=self.policy_path,
            workdir=self.workdir,
        )
        image_info = f", image: {self.image}" if self.image else ""
        log.section(f"Creating sandbox (policy: {resolved_policy}{image_info})")
        sandbox.create(image=self.image, policy_path=resolved_policy)

        self._upload_credentials()

    def stop(self):
        if not sandbox.exists():
            log.section("No sandbox to stop")
            return

        sandbox.delete()
        log.section("Sandbox deleted")

    def run(
        self,
        prompt,
        model,
        streaming=True,
        otel_port=None,
        otel_rate_file=None,
        extra_args=None,
    ):
        self._write_env_script(otel_port, otel_rate_file)
        agent_args = self.harness.build_args(prompt, model, extra_args)

        cmd = ["bash", "-c", f'. {self._ENV_SCRIPT} && exec "$@"', "--", *agent_args]
        proc = sandbox.exec_cmd_streaming(cmd)

        rc = self._process_stream(proc, streaming)
        self._wait_for_otel_flush(otel_port)
        return rc

    def _write_env_script(self, otel_port=None, otel_rate_file=None):
        """Write env vars to a script inside the sandbox, sourced before the agent runs."""
        lines = self.harness.build_env_script_lines(otel_port, otel_rate_file)
        script = "\n".join(lines) + "\n"
        sandbox.exec_cmd(["bash", "-c", f"cat > {self._ENV_SCRIPT} << 'ENVEOF'\n{script}ENVEOF"])

    def _upload_credentials(self):
        if self.harness.auth_mode == "api-key":
            log.section("Using API key auth (skipping credential upload)")
            return

        adc = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        if os.path.isfile(adc):
            source = "default ADC file"
        else:
            adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            source = "GOOGLE_APPLICATION_CREDENTIALS file"
        if not adc or not os.path.isfile(adc):
            log.section("No credentials found to upload")
            return

        log.section(f"Uploading credentials ({source})")

        staging = tempfile.mkdtemp(prefix="agentic-ci-creds-")
        try:
            gcloud_dir = os.path.join(staging, "gcloud")
            os.makedirs(gcloud_dir, exist_ok=True)
            shutil.copy2(adc, os.path.join(gcloud_dir, "application_default_credentials.json"))

            config_default = os.path.expanduser("~/.config/gcloud/configurations/config_default")
            if os.path.isfile(config_default):
                conf_dest = os.path.join(gcloud_dir, "configurations")
                os.makedirs(conf_dest, exist_ok=True)
                shutil.copy2(config_default, os.path.join(conf_dest, "config_default"))

            sandbox.upload(os.path.join(staging, "gcloud"))
            sandbox.exec_cmd(
                [
                    "bash",
                    "-c",
                    'mkdir -p "$HOME/.config/gcloud/configurations"'
                    ' && cp -r gcloud/* "$HOME/.config/gcloud/"',
                ]
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
