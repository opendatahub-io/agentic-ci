"""Harness abstraction for AI agent CLI tools.

A harness encapsulates everything specific to a particular agent CLI
(Claude Code, OpenCode, etc.): how to build the command, what env vars
it needs, where credentials are mounted, and how to parse its output.
"""

import json
import os
import shlex
from abc import ABC, abstractmethod
from typing import Any

from agentic_ci.stream import ClaudeCodeStreamProcessor, OpenCodeStreamProcessor

_OPENSHELL_GATEWAY_HOST = "10.200.0.1"


class Harness(ABC):
    """Base class for agent CLI harnesses."""

    @property
    def auth_mode(self) -> str:
        """Return 'api-key' if ANTHROPIC_API_KEY is set, else 'vertex'."""
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "api-key"
        return "vertex"

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for log messages."""

    @abstractmethod
    def build_args(self, prompt: str, model: str, extra_args: list[str] | None = None) -> list[str]:
        """Build the CLI argument list to run inside the container."""

    @abstractmethod
    def build_env_args(self) -> list[str]:
        """Return ['--env', 'K=V', ...] pairs for ``podman run`` (PodmanBackend only).

        Container-image ENV vars (config dirs, AGENT_TOOL) are already
        set in the Containerfile, so this method should not override them.
        """

    @abstractmethod
    def build_env_script_lines(
        self,
        otel_port: int | None = None,
        otel_rate_file: str | None = None,
    ) -> list[str]:
        """Return ``export K=V`` lines for the env script (OpenShellBackend only).

        OpenShell extracts the container filesystem but drops OCI ENV
        metadata, so every required env var must be re-injected here.
        Config dirs use ``/sandbox/...`` paths per OpenShell convention.
        """

    @abstractmethod
    def build_otel_exec_env(self, otel_port: int | None = None) -> list[str]:
        """Return ['--env', 'K=V', ...] pairs for podman exec when OTEL is enabled."""

    @abstractmethod
    def credential_mount_target(self) -> str:
        """Container-side home directory for credential mounts."""

    @abstractmethod
    def create_stream_processor(self, pid: int = 0) -> Any:
        """Return a stream processor for this harness's output format."""

    @abstractmethod
    def image_env_var(self) -> str:
        """Env var name for the fallback container image."""

    @abstractmethod
    def model_env_var(self) -> str:
        """Env var name for the model override."""

    @abstractmethod
    def default_model(self) -> str:
        """Default model when no --model flag or env var is set."""

    @property
    def supports_otel(self) -> bool:
        """Whether the agent CLI supports OTEL telemetry export."""
        return False

    @property
    def autoupdater_env_var(self) -> str:
        """Env var name to disable auto-updates."""
        return "DISABLE_AUTOUPDATER"

    def write_sandbox_config(self, config_dir, otel_enabled=False):
        """Write agent-specific config files to the sandbox config dir.

        Called by backends before container start. Default is a no-op.
        """


class ClaudeCodeHarness(Harness):
    """Claude Code CLI harness."""

    @property
    def name(self) -> str:
        return "Claude Code"

    def build_args(self, prompt, model, extra_args=None):
        args = [
            "claude",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "-p",
            prompt,
        ]
        if extra_args:
            args.extend(extra_args)
        return args

    def build_env_args(self):
        common = [
            "--env",
            "AGENT_TOOL=claude",
            "--env",
            "CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1",
            "--env",
            "DISABLE_AUTOUPDATER=1",
        ]
        if self.auth_mode == "api-key":
            return [
                "--env",
                "ANTHROPIC_API_KEY",
                *common,
            ]
        vertex_project = os.environ.get(
            "ANTHROPIC_VERTEX_PROJECT_ID", os.environ.get("GCP_PROJECT_ID", "")
        )
        return [
            "--env",
            "CLAUDE_CODE_USE_VERTEX=1",
            "--env",
            f"CLOUD_ML_REGION={os.environ.get('CLOUD_ML_REGION', 'global')}",
            "--env",
            f"ANTHROPIC_VERTEX_PROJECT_ID={vertex_project}",
            *common,
        ]

    def build_env_script_lines(self, otel_port=None, otel_rate_file=None):
        common = [
            "export AGENT_TOOL=claude",
            "export CLAUDE_CONFIG_DIR=/sandbox/.claude",
            "export CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1",
            "export DISABLE_AUTOUPDATER=1",
        ]
        if self.auth_mode == "api-key":
            lines = [
                f"export ANTHROPIC_API_KEY={shlex.quote(os.environ['ANTHROPIC_API_KEY'])}",
                *common,
            ]
        else:
            vertex_project = os.environ.get(
                "ANTHROPIC_VERTEX_PROJECT_ID", os.environ.get("GCP_PROJECT_ID", "")
            )
            cloud_region = os.environ.get("CLOUD_ML_REGION", "global")
            lines = [
                "export CLAUDE_CODE_USE_VERTEX=1",
                f"export CLOUD_ML_REGION={shlex.quote(cloud_region)}",
                f"export ANTHROPIC_VERTEX_PROJECT_ID={shlex.quote(vertex_project)}",
                *common,
            ]
        if otel_port:
            lines.extend(
                [
                    "export CLAUDE_CODE_ENABLE_TELEMETRY=1",
                    "export OTEL_METRICS_EXPORTER=otlp",
                    "export OTEL_LOGS_EXPORTER=otlp",
                    "export OTEL_TRACES_EXPORTER=otlp",
                    "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
                    f"export OTEL_EXPORTER_OTLP_ENDPOINT=http://{_OPENSHELL_GATEWAY_HOST}:{otel_port}",
                    "export OTEL_METRIC_EXPORT_INTERVAL=10000",
                    "export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1",
                    "export OTEL_LOG_USER_PROMPTS=1",
                    "export OTEL_LOG_TOOL_DETAILS=1",
                    "export OTEL_LOG_TOOL_CONTENT=1",
                ]
            )
        return lines

    def build_otel_exec_env(self, otel_port=None):
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
            "OTEL_TRACES_EXPORTER=otlp",
            "--env",
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
            "--env",
            f"OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{otel_port}",
            "--env",
            "OTEL_METRIC_EXPORT_INTERVAL=10000",
            "--env",
            "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1",
            "--env",
            "OTEL_LOG_USER_PROMPTS=1",
            "--env",
            "OTEL_LOG_TOOL_DETAILS=1",
            "--env",
            "OTEL_LOG_TOOL_CONTENT=1",
        ]

    def credential_mount_target(self):
        return os.environ.get("CLAUDE_CONTAINER_HOME", "/home/agent-ci")

    def create_stream_processor(self, pid=0):
        return ClaudeCodeStreamProcessor(claude_pid=pid)

    def image_env_var(self):
        return "CLAUDE_CONTAINER_IMAGE"

    def model_env_var(self):
        return "CLAUDE_MODEL"

    def default_model(self):
        return "claude-opus-4-6"

    @property
    def supports_otel(self) -> bool:
        return True


class OpenCodeHarness(Harness):
    """OpenCode CLI harness."""

    @property
    def name(self) -> str:
        return "OpenCode"

    def build_args(self, prompt, model, extra_args=None):
        args = [
            "opencode",
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
            "-m",
            model,
            prompt,
        ]
        if extra_args:
            args.extend(extra_args)
        return args

    def build_env_args(self):
        common = [
            "--env",
            "AGENT_TOOL=opencode",
            "--env",
            "OPENCODE_DISABLE_AUTOUPDATE=1",
        ]
        if self.auth_mode == "api-key":
            return [
                "--env",
                "ANTHROPIC_API_KEY",
                *common,
            ]
        project = os.environ.get(
            "GOOGLE_CLOUD_PROJECT",
            os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", os.environ.get("GCP_PROJECT_ID", "")),
        )
        location = os.environ.get(
            "VERTEX_LOCATION",
            os.environ.get("CLOUD_ML_REGION", "global"),
        )
        mount_target = self.credential_mount_target()
        return [
            "--env",
            f"GOOGLE_CLOUD_PROJECT={project}",
            "--env",
            f"VERTEX_LOCATION={location}",
            "--env",
            f"GOOGLE_APPLICATION_CREDENTIALS={mount_target}/.config/gcloud/application_default_credentials.json",
            *common,
        ]

    def build_env_script_lines(self, otel_port=None, otel_rate_file=None):
        common = [
            "export AGENT_TOOL=opencode",
            "export OPENCODE_CONFIG_DIR=/sandbox/.config/opencode",
            "export OPENCODE_DISABLE_AUTOUPDATE=1",
        ]
        if self.auth_mode == "api-key":
            lines = [
                f"export ANTHROPIC_API_KEY={shlex.quote(os.environ['ANTHROPIC_API_KEY'])}",
                *common,
            ]
        else:
            project = os.environ.get(
                "GOOGLE_CLOUD_PROJECT",
                os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", os.environ.get("GCP_PROJECT_ID", "")),
            )
            location = os.environ.get(
                "VERTEX_LOCATION",
                os.environ.get("CLOUD_ML_REGION", "global"),
            )
            lines = [
                f"export GOOGLE_CLOUD_PROJECT={shlex.quote(project)}",
                f"export VERTEX_LOCATION={shlex.quote(location)}",
                *common,
            ]
        if otel_port:
            lines.extend(
                [
                    f"export OTEL_EXPORTER_OTLP_ENDPOINT=http://{_OPENSHELL_GATEWAY_HOST}:{otel_port}",
                    "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
                    "export OTEL_BSP_SCHEDULE_DELAY=0",
                ]
            )
        return lines

    def build_otel_exec_env(self, otel_port=None):
        if not otel_port:
            return []
        return [
            "--env",
            f"OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{otel_port}",
            "--env",
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
            "--env",
            "OTEL_BSP_SCHEDULE_DELAY=0",
        ]

    def credential_mount_target(self):
        return os.environ.get("OPENCODE_CONTAINER_HOME", "/home/agent-ci")

    def create_stream_processor(self, pid=0):
        return OpenCodeStreamProcessor(agent_pid=pid)

    def image_env_var(self):
        return "OPENCODE_CONTAINER_IMAGE"

    def model_env_var(self):
        return "OPENCODE_MODEL"

    def default_model(self):
        return "google-vertex/claude-opus-4-6@default"

    @property
    def supports_otel(self) -> bool:
        return True

    @property
    def autoupdater_env_var(self):
        return "OPENCODE_DISABLE_AUTOUPDATE"

    def write_sandbox_config(self, config_dir, otel_enabled=False):
        opencode_dir = os.path.join(config_dir, ".config", "opencode")
        os.makedirs(opencode_dir, exist_ok=True)
        config = {"$schema": "https://opencode.ai/config.json"}
        if otel_enabled:
            config["experimental"] = {"openTelemetry": True}
        with open(os.path.join(opencode_dir, "opencode.json"), "w") as f:
            json.dump(config, f, indent=2)


def create_harness(name: str) -> Harness:
    """Create a harness instance by name."""
    if name == "claude-code":
        return ClaudeCodeHarness()
    elif name == "opencode":
        return OpenCodeHarness()
    else:
        raise ValueError(f"Unknown harness: {name!r}. Choose 'claude-code' or 'opencode'.")
