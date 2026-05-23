"""Harness abstraction for AI agent CLI tools.

A harness encapsulates everything specific to a particular agent CLI
(Claude Code, OpenCode, etc.): how to build the command, what env vars
it needs, where credentials are mounted, and how to parse its output.
"""

import os
import shlex
from abc import ABC, abstractmethod
from typing import Any


class Harness(ABC):
    """Base class for agent CLI harnesses."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for log messages."""

    @abstractmethod
    def build_args(self, prompt: str, model: str, extra_args: list[str] | None = None) -> list[str]:
        """Build the CLI argument list to run inside the container."""

    @abstractmethod
    def build_env_args(self) -> list[str]:
        """Return ['--env', 'K=V', ...] pairs for podman run."""

    @abstractmethod
    def build_env_script_lines(
        self,
        otel_port: int | None = None,
        otel_rate_file: str | None = None,
    ) -> list[str]:
        """Return 'export K=V' lines for the OpenShell env script."""

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

    def build_env_script_lines(self, otel_port=None, otel_rate_file=None):
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
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
            "--env",
            f"OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{otel_port}",
            "--env",
            "OTEL_METRIC_EXPORT_INTERVAL=10000",
        ]

    def credential_mount_target(self):
        return "/home/claude"

    def create_stream_processor(self, pid=0):
        from agentic_ci.stream import ClaudeCodeStreamProcessor

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
        project = os.environ.get(
            "GOOGLE_CLOUD_PROJECT",
            os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", ""),
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
            "OPENCODE_DISABLE_AUTOUPDATE=1",
            "--env",
            f"GOOGLE_APPLICATION_CREDENTIALS={mount_target}/.config/gcloud/application_default_credentials.json",
        ]

    def build_env_script_lines(self, otel_port=None, otel_rate_file=None):
        project = os.environ.get(
            "GOOGLE_CLOUD_PROJECT",
            os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", ""),
        )
        location = os.environ.get(
            "VERTEX_LOCATION",
            os.environ.get("CLOUD_ML_REGION", "global"),
        )
        lines = [
            f"export GOOGLE_CLOUD_PROJECT={shlex.quote(project)}",
            f"export VERTEX_LOCATION={shlex.quote(location)}",
            "export OPENCODE_DISABLE_AUTOUPDATE=1",
            "export GOOGLE_APPLICATION_CREDENTIALS="
            '"$HOME/.config/gcloud/application_default_credentials.json"',
        ]
        return lines

    def build_otel_exec_env(self, otel_port=None):
        return []

    def credential_mount_target(self):
        return "/home/agent"

    def create_stream_processor(self, pid=0):
        from agentic_ci.stream import OpenCodeStreamProcessor

        return OpenCodeStreamProcessor(agent_pid=pid)

    def image_env_var(self):
        return "OPENCODE_CONTAINER_IMAGE"

    def model_env_var(self):
        return "OPENCODE_MODEL"

    def default_model(self):
        return "google-vertex/claude-sonnet-4-6@default"


def create_harness(name: str) -> Harness:
    """Create a harness instance by name."""
    if name == "claude-code":
        return ClaudeCodeHarness()
    elif name == "opencode":
        return OpenCodeHarness()
    else:
        raise ValueError(f"Unknown harness: {name!r}. Choose 'claude-code' or 'opencode'.")
