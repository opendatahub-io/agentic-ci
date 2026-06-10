"""Podman container backend for agentic-ci."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from typing import TYPE_CHECKING

from agentic_ci import log
from agentic_ci.backend import Backend

if TYPE_CHECKING:
    from agentic_ci.harness import Harness

CONTAINER_NAME = "agentic-ci"


class PodmanBackend(Backend):
    """Runs an AI agent inside a persistent Podman container.

    setup() creates a long-running detached container. run() execs
    the agent inside it. stop() tears it down. The work directory is
    mounted into the container and gcloud credentials are mounted
    read-only.
    """

    def __init__(
        self,
        workdir=".",
        image=None,
        timeout=1200,
        extra_env=None,
        *,
        harness: Harness,
    ):
        super().__init__(workdir=workdir, image=image, harness=harness)
        self.timeout = timeout
        self._config_dir = None
        self._extra_env = extra_env or {}

    def setup(self, otel_port=None):
        self._resolve_image()
        if self.harness.auth_mode == "vertex":
            self._resolve_credentials()

        if self.is_running():
            log.section("Podman container already running")
            return

        subprocess.run(["podman", "rm", "-f", CONTAINER_NAME], capture_output=True)

        if os.getuid() == 0:
            log.detail("Container user", "root (chown workdir)")
            subprocess.run(
                ["chown", "-R", "1000:1000", self.workdir],
                capture_output=True,
            )
        else:
            log.detail("Container user", "rootless (userns keep-id)")

        env_args = self._build_env_args()
        vol_args = self._build_vol_args()

        user_args = (
            ["--user", "1000:1000"] if os.getuid() == 0 else ["--userns=keep-id:uid=1000,gid=1000"]
        )

        if self._is_local_image():
            log.section("Local image, skipping pull")
        else:
            log.section("Pulling image")
            proc = subprocess.Popen(
                ["podman", "pull", self.image],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            for line in proc.stdout:
                log.info(line.decode("utf-8", errors="replace").rstrip())
            rc = proc.wait()
            if rc != 0:
                raise subprocess.CalledProcessError(rc, ["podman", "pull", self.image])

        cmd = [
            "podman",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--pull",
            "never",
            "--network",
            "host",
            *user_args,
            *env_args,
            *vol_args,
            "--workdir",
            "/workspace",
            self.image,
            "bash",
            "-c",
            f"sleep {self.timeout}",
        ]

        subprocess.run(cmd, check=True, capture_output=True)
        log.section("Podman container started")

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

        log.section(f"Executing {self.harness.name} in container")
        otel_env = self.harness.build_otel_exec_env(otel_port)
        agent_args = self.harness.build_args(prompt, model, extra_args)

        proc = subprocess.Popen(
            [
                "podman",
                "exec",
                "--env",
                f"AGENT_MODEL={model}",
                *otel_env,
                CONTAINER_NAME,
                *agent_args,
            ],
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
            log.section("Podman container stopped")
        else:
            stderr = (result.stderr or b"").decode("utf-8", errors="replace")
            if "no such container" in stderr.lower():
                log.section("No container to stop")
            else:
                raise subprocess.CalledProcessError(
                    result.returncode, ["podman", "rm", "-f", CONTAINER_NAME], stderr=result.stderr
                )

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

    def _is_local_image(self):
        """Check if the image reference is local-only (not from a remote registry).

        Images prefixed with 'localhost/' are local podman builds.
        All other references (registry.example.com/..., ghcr.io/...,
        or bare names) are treated as remote.
        """
        return self.image.startswith("localhost/")

    def _resolve_image(self):
        if not self.image:
            env_var = self.harness.image_env_var()
            self.image = os.environ.get(env_var)
            if not self.image:
                raise RuntimeError(f"No container image specified. Use --image or set {env_var}.")
        log.detail("Image", self.image)

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

        creds_json, creds_source = self._find_credentials()
        adc_path = os.path.join(
            self._config_dir, ".config", "gcloud", "application_default_credentials.json"
        )
        with open(adc_path, "w") as f:
            f.write(creds_json)

        log.section(f"Credentials staged ({creds_source})")

    def _find_credentials(self):
        """Locate and validate gcloud credentials. Returns (json_string, source_label)."""
        raw = os.environ.get("GCLOUD_CREDENTIALS", "")
        if raw:
            if self._is_valid_json(raw):
                return raw, "GCLOUD_CREDENTIALS env var"
            decoded = self._try_base64_decode(raw)
            if decoded and self._is_valid_json(decoded):
                return decoded, "GCLOUD_CREDENTIALS env var (base64)"
            raise RuntimeError("GCLOUD_CREDENTIALS is not valid JSON or base64-encoded JSON")

        sa_key = os.environ.get("GCP_SERVICE_ACCOUNT_KEY", "")
        if sa_key:
            if os.path.isfile(sa_key):
                try:
                    with open(sa_key) as f:
                        content = f.read().strip()
                except OSError as exc:
                    raise RuntimeError(
                        f"Failed to read GCP_SERVICE_ACCOUNT_KEY file {sa_key}: {exc}"
                    ) from exc
                sa_key = content
                source_label = "GCP_SERVICE_ACCOUNT_KEY file"
            else:
                source_label = "GCP_SERVICE_ACCOUNT_KEY env var"
            if self._is_valid_json(sa_key):
                return sa_key, source_label
            decoded = self._try_base64_decode(sa_key)
            if decoded and self._is_valid_json(decoded):
                return decoded, source_label
            raise RuntimeError("GCP_SERVICE_ACCOUNT_KEY is not valid JSON or base64-encoded JSON")

        adc = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        ga_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        for path, label in [
            (adc, "default ADC file"),
            (ga_creds, "GOOGLE_APPLICATION_CREDENTIALS file"),
        ]:
            if path and os.path.isfile(path):
                with open(path) as f:
                    content = f.read()
                if self._is_valid_json(content):
                    return content, label

        raise RuntimeError(
            "No GCP credentials found. Set GCLOUD_CREDENTIALS, "
            "GCP_SERVICE_ACCOUNT_KEY, or configure gcloud ADC."
        )

    def _build_env_args(self):
        args = list(self.harness.build_env_args())
        for key, val in self._extra_env.items():
            args.extend(["--env", f"{key}={val}"])
        return args

    def _build_vol_args(self):
        vols = ["-v", f"{self.workdir}:/workspace:z"]
        if self._config_dir is not None:
            mount_target = self.harness.credential_mount_target()
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
            vols.extend(
                [
                    "-v",
                    f"{adc}:{mount_target}/.config/gcloud/application_default_credentials.json:ro,z",
                    "-v",
                    f"{config}:{mount_target}/.config/gcloud/configurations/config_default:ro,z",
                ]
            )
        return vols

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
