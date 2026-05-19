"""Podman container backend for agentic-ci."""

import base64
import json
import os
import subprocess
import tempfile

from agentic_ci.backend import Backend

CONTAINER_NAME = "agentic-ci"


class PodmanBackend(Backend):
    """Runs Claude Code inside a persistent Podman container.

    setup() creates a long-running detached container. run() execs
    Claude inside it. stop() tears it down. The work directory is
    mounted into the container and gcloud credentials are mounted
    read-only.
    """

    def __init__(self, workdir=".", image=None, timeout=1200):
        super().__init__(workdir=workdir, image=image)
        self.timeout = timeout
        self._config_dir = None

    def setup(self):
        self._resolve_image()
        self._resolve_credentials()

        if self.is_running():
            print("--- Podman container already running ---", flush=True)
            return

        subprocess.run(["podman", "rm", "-f", CONTAINER_NAME], capture_output=True)

        if os.getuid() == 0:
            subprocess.run(
                ["chown", "-R", "1000:1000", self.workdir],
                capture_output=True,
            )

        env_args = self._build_env_args()
        vol_args = self._build_vol_args()

        user_args = (
            ["--user", "1000:1000"] if os.getuid() == 0 else ["--userns=keep-id:uid=1000,gid=1000"]
        )

        cmd = [
            "podman",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--pull",
            "newer",
            "--network",
            "host",
            *user_args,
            *env_args,
            *vol_args,
            "--workdir",
            "/workspace",
            "--entrypoint",
            "sleep",
            self.image,
            str(self.timeout),
        ]

        subprocess.run(cmd, check=True, capture_output=True)
        print("--- Podman container started ---", flush=True)

    def run(
        self,
        prompt,
        model,
        streaming=True,
        otel_port=None,
        otel_rate_file=None,
        extra_args=None,
    ):
        if not self.is_running():
            self.setup()

        otel_env = self._build_otel_exec_env(otel_port)
        claude_args = self._build_claude_args(prompt, model, extra_args)

        proc = subprocess.Popen(
            ["podman", "exec", *otel_env, CONTAINER_NAME, *claude_args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        rc = self._process_stream(proc, streaming)
        self._wait_for_otel_flush(otel_port)
        return rc

    def stop(self):
        result = subprocess.run(
            ["podman", "rm", "-f", CONTAINER_NAME],
            capture_output=True,
        )
        if result.returncode == 0:
            print("--- Podman container stopped ---", flush=True)
        else:
            print("--- No container to stop ---", flush=True)

    def is_running(self):
        result = subprocess.run(
            [
                "podman",
                "container",
                "inspect",
                "--format",
                "{{.State.Running}}",
                CONTAINER_NAME,
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _resolve_image(self):
        if not self.image:
            self.image = os.environ.get("CLAUDE_CONTAINER_IMAGE")
            if not self.image:
                raise RuntimeError(
                    "No container image specified. Use --image or set CLAUDE_CONTAINER_IMAGE."
                )

    def _resolve_credentials(self):
        if self._config_dir is not None:
            return

        self._config_dir = tempfile.mkdtemp(prefix="agentic-ci-podman-")

        gcloud_dir = os.path.join(self._config_dir, ".config", "gcloud", "configurations")
        os.makedirs(gcloud_dir, exist_ok=True)

        vertex_project = os.environ.get(
            "ANTHROPIC_VERTEX_PROJECT_ID",
            os.environ.get("GCP_PROJECT_ID", ""),
        )
        config_path = os.path.join(gcloud_dir, "config_default")
        with open(config_path, "w") as f:
            f.write(f"[core]\nproject = {vertex_project}\ndisable_prompts = true\n")

        creds_json = self._find_credentials()
        adc_path = os.path.join(
            self._config_dir, ".config", "gcloud", "application_default_credentials.json"
        )
        with open(adc_path, "w") as f:
            f.write(creds_json)

        print("--- Credentials staged ---", flush=True)

    def _find_credentials(self):
        """Locate and validate gcloud credentials. Returns raw JSON string."""
        raw = os.environ.get("GCLOUD_CREDENTIALS", "")
        if raw:
            if self._is_valid_json(raw):
                return raw
            decoded = self._try_base64_decode(raw)
            if decoded and self._is_valid_json(decoded):
                return decoded
            raise RuntimeError("GCLOUD_CREDENTIALS is not valid JSON or base64-encoded JSON")

        sa_key = os.environ.get("GCP_SERVICE_ACCOUNT_KEY", "")
        if sa_key:
            decoded = self._try_base64_decode(sa_key)
            if decoded and self._is_valid_json(decoded):
                return decoded
            raise RuntimeError("GCP_SERVICE_ACCOUNT_KEY is not valid base64-encoded JSON")

        adc = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        ga_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        for path in [adc, ga_creds]:
            if path and os.path.isfile(path):
                with open(path) as f:
                    content = f.read()
                if self._is_valid_json(content):
                    return content

        raise RuntimeError(
            "No GCP credentials found. Set GCLOUD_CREDENTIALS, "
            "GCP_SERVICE_ACCOUNT_KEY, or configure gcloud ADC."
        )

    def _build_env_args(self):
        return [
            "--env",
            "CLAUDE_CODE_USE_VERTEX=1",
            "--env",
            f"CLOUD_ML_REGION={os.environ.get('CLOUD_ML_REGION', 'global')}",
            "--env",
            f"ANTHROPIC_VERTEX_PROJECT_ID={os.environ.get('ANTHROPIC_VERTEX_PROJECT_ID', '')}",
            "--env",
            "DISABLE_AUTOUPDATER=1",
        ]

    def _build_otel_exec_env(self, otel_port=None):
        """Build --env flags for podman exec when OTEL is enabled."""
        if not otel_port:
            return []
        return [
            "--env",
            "CLAUDE_CODE_ENABLE_TELEMETRY=1",
            "--env",
            "OTEL_METRICS_EXPORTER=otlp",
            "--env",
            "OTEL_LOGS_EXPORTER=otlp",
            "--env",
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
            "--env",
            f"OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{otel_port}",
            "--env",
            "OTEL_METRIC_EXPORT_INTERVAL=10000",
        ]

    def _build_vol_args(self):
        assert self._config_dir is not None
        adc = os.path.join(
            self._config_dir,
            ".config",
            "gcloud",
            "application_default_credentials.json",
        )
        config = os.path.join(
            self._config_dir,
            ".config",
            "gcloud",
            "configurations",
            "config_default",
        )
        return [
            "-v",
            f"{adc}:/home/claude/.config/gcloud/application_default_credentials.json:ro,z",
            "-v",
            f"{config}:/home/claude/.config/gcloud/configurations/config_default:ro,z",
            "-v",
            f"{self.workdir}:/workspace:z",
        ]

    @staticmethod
    def _is_valid_json(text):
        try:
            json.loads(text)
            return True
        except (json.JSONDecodeError, ValueError):
            return False

    @staticmethod
    def _try_base64_decode(text):
        try:
            return base64.b64decode(text).decode("utf-8")
        except Exception:
            return None
