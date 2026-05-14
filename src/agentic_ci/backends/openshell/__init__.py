"""OpenShell sandbox backend for agentic-ci."""

import os
import shlex
import shutil
import tempfile

from agentic_ci.backend import Backend
from agentic_ci.backends.openshell import gateway, policy, sandbox


class OpenShellBackend(Backend):
    """Runs Claude Code inside an OpenShell sandbox.

    OpenShell provides security-focused sandboxing with network policy
    enforcement, filesystem isolation, and Landlock-based access control.
    """

    _ENV_SCRIPT = "/tmp/.agentic-ci-env.sh"

    def __init__(self, workdir=".", image=None, policy=None):
        super().__init__(workdir=workdir, image=image)
        self.policy_path = policy

    def setup(self):
        if not gateway.is_running():
            print("--- Starting OpenShell gateway ---", flush=True)
            gateway.start()
        else:
            print("--- OpenShell gateway already running ---", flush=True)

        if sandbox.exists():
            print("--- Sandbox already exists ---", flush=True)
            return

        resolved_policy = policy.resolve(
            flag_path=self.policy_path,
            workdir=self.workdir,
        )
        print(f"--- Creating sandbox (policy: {resolved_policy}) ---", flush=True)
        sandbox.create(image=self.image, policy_path=resolved_policy)

        print("--- Uploading credentials ---", flush=True)
        self._upload_credentials()

    def stop(self):
        if not sandbox.exists():
            print("--- No sandbox to stop ---", flush=True)
            return

        sandbox.delete()
        print("--- Sandbox deleted ---", flush=True)

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
        claude_args = self._build_claude_args(prompt, model, extra_args)

        cmd = ["bash", "-c", f'. {self._ENV_SCRIPT} && exec "$@"', "--", *claude_args]
        proc = sandbox.exec_cmd_streaming(cmd)

        rc = self._process_stream(proc, streaming)
        self._wait_for_otel_flush(otel_port)
        return rc

    def _write_env_script(self, otel_port=None, otel_rate_file=None):
        """Write env vars to a script inside the sandbox, sourced before Claude runs."""
        vertex_project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
        cloud_region = os.environ.get("CLOUD_ML_REGION", "global")
        lines = [
            "export CLAUDE_CODE_USE_VERTEX=1",
            f"export CLOUD_ML_REGION={shlex.quote(cloud_region)}",
            f"export ANTHROPIC_VERTEX_PROJECT_ID={shlex.quote(vertex_project)}",
            "export DISABLE_AUTOUPDATER=1",
            "export GOOGLE_APPLICATION_CREDENTIALS="
            '"$HOME/.config/gcloud/application_default_credentials.json"',
        ]
        if otel_port:
            lines.extend(
                [
                    "export CLAUDE_CODE_ENABLE_TELEMETRY=1",
                    "export OTEL_METRICS_EXPORTER=otlp",
                    "export OTEL_LOGS_EXPORTER=otlp",
                    "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
                    f"export OTEL_EXPORTER_OTLP_ENDPOINT=http://10.200.0.1:{otel_port}",
                    "export OTEL_METRIC_EXPORT_INTERVAL=10000",
                ]
            )
            if otel_rate_file:
                lines.append(f"export OTEL_RATE_FILE={otel_rate_file}")

        script = "\n".join(lines) + "\n"
        sandbox.exec_cmd(["bash", "-c", f"cat > {self._ENV_SCRIPT} << 'ENVEOF'\n{script}ENVEOF"])

    def _upload_credentials(self):
        adc = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        if not os.path.isfile(adc):
            adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not adc or not os.path.isfile(adc):
            return

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
