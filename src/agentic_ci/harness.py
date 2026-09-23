"""Harness abstraction for AI agent CLI tools.

A harness encapsulates everything specific to a particular agent CLI
(Claude Code, OpenCode, Codex, etc.): how to build the command, what env vars
it needs, where credentials are mounted, and how to parse its output.
"""

import json
import os
import shlex
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentic_ci.models import HarnessModels, ModelTier, harness_models
from agentic_ci.stream import (
    ClaudeCodeStreamProcessor,
    CodexStreamProcessor,
    OpenCodeStreamProcessor,
)

_OPENSHELL_GATEWAY_HOST = "10.200.0.1"

EFFORT_NONE = "none"
"""Effort override value meaning "pass no effort flag"."""

AGENT_EFFORT_ENV_VAR = "AGENT_REASONING_EFFORT"
"""Env var carrying the effective reasoning effort into the agent environment.

Backends export it next to ``AGENT_MODEL`` so skills can read the effort in
use without knowing the harness. Like ``AGENT_MODEL`` it is output only:
:meth:`Harness.resolve_efforts` never reads it, so an agentic-ci run started
inside an agent does not inherit the outer run's effort.
"""


class Harness(ABC):
    """Base class for agent CLI harnesses."""

    @property
    def auth_mode(self) -> str:
        """Return 'api-key' if ANTHROPIC_API_KEY is set, else 'vertex'."""
        return self.auth_mode_for_env(os.environ)

    def auth_mode_for_env(self, env: Mapping[str, str] | None = None) -> str:
        """Return the authentication mode selected by *env*."""
        credential_env = env if env is not None else os.environ
        if credential_env.get("ANTHROPIC_API_KEY"):
            return "api-key"
        return "vertex"

    def validate_credentials(
        self,
        env: Mapping[str, str] | None = None,
        *,
        allow_auth_file: bool = False,
    ) -> None:
        """Fail early when harness-specific credentials are unavailable.

        Harnesses without additional validation requirements use this no-op
        implementation.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for log messages."""

    @abstractmethod
    def build_args(
        self,
        prompt: str,
        model: str,
        extra_args: list[str] | None = None,
        otel_endpoint: str | None = None,
        externally_sandboxed: bool = False,
    ) -> list[str]:
        """Build the CLI argument list to run inside the container.

        ``externally_sandboxed`` is True when the backend already isolates the
        agent (OpenShell), so a harness may drop its own approval prompts and
        inner sandbox. Harnesses that always bypass permissions ignore it.
        """

    @abstractmethod
    def build_env_args(self, env: Mapping[str, str] | None = None) -> list[str]:
        """Return ['--env', 'K=V', ...] pairs for ``podman run`` (PodmanBackend only).

        Container-image ENV vars (config dirs, AGENT_TOOL) are already
        set in the Containerfile, so this method should not override them.
        """

    @abstractmethod
    def build_env_script_lines(
        self,
        otel_port: int | None = None,
        otel_rate_file: str | None = None,
        traceparent: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Return ``export K=V`` lines for the env script (OpenShellBackend only).

        OpenShell extracts the container filesystem but drops OCI ENV
        metadata, so every required env var must be re-injected here.
        Config dirs use ``/sandbox/...`` paths per OpenShell convention.
        """

    @abstractmethod
    def build_otel_exec_env(
        self, otel_port: int | None = None, traceparent: str | None = None
    ) -> list[str]:
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

    registry_key: str = ""
    """Key of this harness in ``agentic_ci.models.MODEL_REGISTRY``."""

    @property
    def models(self) -> HarnessModels:
        """Model ids and effort levels for this harness, from the registry."""
        return harness_models(self.registry_key)

    def default_model(self) -> str:
        """Default model when no --model flag or env var is set."""
        return self.models.default

    def default_model_tiers(self) -> dict[str, ModelTier]:
        """Registry ``low``/``medium``/``high`` routing tiers for this harness.

        The ``high`` tier matches :meth:`default_model` so routed runs never
        regress hard tasks. ``SkillConfig.model_tiers`` overrides entries.
        """
        return dict(self.models.tiers)

    @abstractmethod
    def effort_env_var(self) -> str:
        """Env var name for the reasoning effort override."""

    def subagent_effort_env_var(self) -> str | None:
        """Env var name for the sub-agent effort override (``None`` = no separate knob)."""
        return None

    def resolve_efforts(
        self, effort: str | None = None, env: Mapping[str, str] | None = None
    ) -> tuple[str | None, str | None]:
        """Return the effective ``(effort, subagent_effort)`` for a run.

        Precedence for the main effort: explicit *effort* argument, then the
        :meth:`effort_env_var` value, then the registry ``default_effort``.
        The sub-agent effort is the :meth:`subagent_effort_env_var` value,
        then the registry ``subagent_effort``, then the main effort; it is
        ``None`` for harnesses without a separate sub-agent knob. The literal
        value ``none`` selects "no effort flag". Values are validated by
        :meth:`build_effort_args`.
        """
        config_env = env if env is not None else os.environ
        main = effort or config_env.get(self.effort_env_var()) or self.models.default_effort
        if main == EFFORT_NONE:
            main = None
        sub_var = self.subagent_effort_env_var()
        if sub_var is None:
            return main, None
        sub = config_env.get(sub_var) or self.models.subagent_effort or main
        if sub == EFFORT_NONE:
            sub = None
        return main, sub

    def build_effort_args(
        self, effort: str | None, subagent_effort: str | None = None
    ) -> list[str]:
        """Return CLI args that set reasoning effort for this harness.

        Returns ``[]`` when both values are ``None``. Raises ``ValueError``
        for a value outside the registry's ``efforts`` set, so an invalid
        override fails before the agent starts. The result is passed to
        ``build_args()`` through ``extra_args``.
        """
        for label, value in (("effort", effort), ("sub-agent effort", subagent_effort)):
            if value is not None and value not in self.models.efforts:
                raise ValueError(
                    f"Unsupported {self.name} {label} {value!r}; "
                    f"expected one of {sorted(self.models.efforts)} or {EFFORT_NONE!r}"
                )
        if effort is None and subagent_effort is None:
            return []
        return self.effort_args(effort, subagent_effort)

    @abstractmethod
    def effort_args(self, effort: str | None, subagent_effort: str | None) -> list[str]:
        """CLI fragment for already-validated efforts.

        Harnesses without a sub-agent knob ignore *subagent_effort*.
        """

    def classifier_effort(self) -> str | None:
        """Effort for the classifier run, from the registry.

        ``None`` means normal resolution (effort env var, else ``default_effort``).
        """
        return self.models.classifier_effort

    def build_classifier_args(self, max_turns: int) -> list[str]:
        """Extra CLI args that bound the classifier run.

        Default is no bound; harnesses whose CLI has a turn cap override this.
        """
        return []

    @abstractmethod
    def build_local_env(
        self,
        otel_port: int | None = None,
        otel_rate_file: str | None = None,
        traceparent: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Return env vars as a plain dict for direct (local) execution.

        Unlike build_env_args (podman --env format) or build_env_script_lines
        (OpenShell export format), this returns a dict suitable for merging
        into os.environ and passing to subprocess.Popen(env=...).
        """

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

    def sandbox_config_mounts(self, config_dir):
        """Return list of (host_path, container_path) for config file mounts.

        Called by backends to mount config files written by write_sandbox_config().
        Default returns empty list.
        """
        return []


class ClaudeCodeHarness(Harness):
    """Claude Code CLI harness."""

    registry_key = "claude-code"

    @property
    def name(self) -> str:
        return "Claude Code"

    def auth_mode_for_env(self, env: Mapping[str, str] | None = None) -> str:
        """Return 'oauth' when CLAUDE_CODE_OAUTH_TOKEN is set and ANTHROPIC_API_KEY is not.

        ANTHROPIC_API_KEY still wins, matching Claude Code's own authentication
        precedence. Only this harness selects 'oauth': a subscription token is a
        Claude Code credential, and OpenCode and Codex have no path to use it.
        """
        credential_env = env if env is not None else os.environ
        mode = super().auth_mode_for_env(credential_env)
        if mode == "vertex" and credential_env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "oauth"
        return mode

    def build_args(
        self, prompt, model, extra_args=None, otel_endpoint=None, externally_sandboxed=False
    ):
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

    def build_env_args(self, env=None):
        credential_env = env if env is not None else os.environ
        common = [
            "--env",
            "AGENT_TOOL=claude",
            "--env",
            "CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1",
            "--env",
            "DISABLE_AUTOUPDATER=1",
        ]
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            common.extend(["--env", f"AGENT_ENABLED_PLUGINS={enabled_plugins}"])
        auth_mode = self.auth_mode_for_env(credential_env)
        if auth_mode == "api-key":
            return [
                "--env",
                "ANTHROPIC_API_KEY",
                *common,
            ]
        if auth_mode == "oauth":
            return [
                "--env",
                "CLAUDE_CODE_OAUTH_TOKEN",
                *common,
            ]
        vertex_project = credential_env.get(
            "ANTHROPIC_VERTEX_PROJECT_ID", credential_env.get("GCP_PROJECT_ID", "")
        )
        return [
            "--env",
            "CLAUDE_CODE_USE_VERTEX=1",
            "--env",
            f"CLOUD_ML_REGION={credential_env.get('CLOUD_ML_REGION', 'global')}",
            "--env",
            f"ANTHROPIC_VERTEX_PROJECT_ID={vertex_project}",
            *common,
        ]

    def build_env_script_lines(
        self, otel_port=None, otel_rate_file=None, traceparent=None, env=None
    ):
        credential_env = env if env is not None else os.environ
        common = [
            "export AGENT_TOOL=claude",
            "export CLAUDE_CONFIG_DIR=/sandbox/.claude",
            "export CLAUDE_CODE_PLUGIN_SEED_DIR=/sandbox/.claude-seed",
            "export CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1",
            "export DISABLE_AUTOUPDATER=1",
        ]
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            common.append(f"export AGENT_ENABLED_PLUGINS={shlex.quote(enabled_plugins)}")
        auth_mode = self.auth_mode_for_env(credential_env)
        if auth_mode == "api-key":
            lines = [
                f"export ANTHROPIC_API_KEY={shlex.quote(credential_env['ANTHROPIC_API_KEY'])}",
                *common,
            ]
        elif auth_mode == "oauth":
            oauth_token = credential_env["CLAUDE_CODE_OAUTH_TOKEN"]
            lines = [
                f"export CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(oauth_token)}",
                *common,
            ]
        else:
            vertex_project = credential_env.get(
                "ANTHROPIC_VERTEX_PROJECT_ID", credential_env.get("GCP_PROJECT_ID", "")
            )
            cloud_region = credential_env.get("CLOUD_ML_REGION", "global")
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
                    "export OTEL_BSP_SCHEDULE_DELAY=1000",
                    "export OTEL_METRIC_EXPORT_INTERVAL=10000",
                    "export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1",
                    "export OTEL_LOG_USER_PROMPTS=1",
                    "export OTEL_LOG_TOOL_DETAILS=1",
                    "export OTEL_LOG_TOOL_CONTENT=1",
                ]
            )
        if traceparent:
            lines.append(f"export TRACEPARENT={shlex.quote(traceparent)}")
        return lines

    def build_otel_exec_env(self, otel_port=None, traceparent=None):
        if not otel_port:
            return []
        env = [
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
            "OTEL_BSP_SCHEDULE_DELAY=1000",
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
        if traceparent:
            env.extend(["--env", f"TRACEPARENT={traceparent}"])
        return env

    def build_local_env(self, otel_port=None, otel_rate_file=None, traceparent=None, env=None):
        credential_env = env if env is not None else os.environ
        env = {
            "AGENT_TOOL": "claude",
            "DISABLE_AUTOUPDATER": "1",
            # -p sets sessionKind, breaking --continue lookup (claude-code#43013)
            "CLAUDE_CODE_ENTRYPOINT": "sdk-cli",
        }
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            env["AGENT_ENABLED_PLUGINS"] = enabled_plugins
        auth_mode = self.auth_mode_for_env(credential_env)
        if auth_mode == "api-key":
            env["ANTHROPIC_API_KEY"] = credential_env["ANTHROPIC_API_KEY"]
        elif auth_mode == "oauth":
            env["CLAUDE_CODE_OAUTH_TOKEN"] = credential_env["CLAUDE_CODE_OAUTH_TOKEN"]
        else:
            env["CLAUDE_CODE_USE_VERTEX"] = "1"
            env["CLOUD_ML_REGION"] = credential_env.get("CLOUD_ML_REGION", "global")
            env["ANTHROPIC_VERTEX_PROJECT_ID"] = credential_env.get(
                "ANTHROPIC_VERTEX_PROJECT_ID", credential_env.get("GCP_PROJECT_ID", "")
            )
        if otel_port:
            env.update(
                {
                    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
                    "OTEL_METRICS_EXPORTER": "otlp",
                    "OTEL_LOGS_EXPORTER": "otlp",
                    "OTEL_TRACES_EXPORTER": "otlp",
                    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{otel_port}",
                    "OTEL_BSP_SCHEDULE_DELAY": "1000",
                    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
                    "OTEL_LOG_USER_PROMPTS": "1",
                    "OTEL_LOG_TOOL_DETAILS": "1",
                    "OTEL_LOG_TOOL_CONTENT": "1",
                }
            )
        if traceparent:
            env["TRACEPARENT"] = traceparent
        return env

    def credential_mount_target(self):
        return os.environ.get("CLAUDE_CONTAINER_HOME", "/home/agent-ci")

    def create_stream_processor(self, pid=0):
        return ClaudeCodeStreamProcessor(claude_pid=pid)

    def image_env_var(self):
        return "CLAUDE_CONTAINER_IMAGE"

    def model_env_var(self):
        return "CLAUDE_MODEL"

    def effort_env_var(self):
        return "CLAUDE_REASONING_EFFORT"

    def effort_args(self, effort, subagent_effort):
        return ["--effort", effort] if effort else []

    def build_classifier_args(self, max_turns):
        return ["--max-turns", str(max_turns)]

    @property
    def supports_otel(self) -> bool:
        return True


class OpenCodeHarness(Harness):
    """OpenCode CLI harness."""

    registry_key = "opencode"

    @property
    def name(self) -> str:
        return "OpenCode"

    def build_args(
        self, prompt, model, extra_args=None, otel_endpoint=None, externally_sandboxed=False
    ):
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

    def build_env_args(self, env=None):
        credential_env = env if env is not None else os.environ
        common = [
            "--env",
            "AGENT_TOOL=opencode",
            "--env",
            f"OPENCODE_CONFIG_DIR={self._CONTAINER_CONFIG_DIR}",
            "--env",
            "OPENCODE_DISABLE_AUTOUPDATE=1",
        ]
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            common.extend(["--env", f"AGENT_ENABLED_PLUGINS={enabled_plugins}"])
        if self.auth_mode_for_env(credential_env) == "api-key":
            return [
                "--env",
                "ANTHROPIC_API_KEY",
                *common,
            ]
        project = credential_env.get(
            "GOOGLE_CLOUD_PROJECT",
            credential_env.get(
                "ANTHROPIC_VERTEX_PROJECT_ID", credential_env.get("GCP_PROJECT_ID", "")
            ),
        )
        location = credential_env.get(
            "VERTEX_LOCATION",
            credential_env.get("CLOUD_ML_REGION", "global"),
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

    def build_env_script_lines(
        self, otel_port=None, otel_rate_file=None, traceparent=None, env=None
    ):
        credential_env = env if env is not None else os.environ
        common = [
            "export AGENT_TOOL=opencode",
            "export OPENCODE_CONFIG_DIR=/sandbox/.config/opencode",
            "export OPENCODE_DISABLE_AUTOUPDATE=1",
        ]
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            common.append(f"export AGENT_ENABLED_PLUGINS={shlex.quote(enabled_plugins)}")
        if self.auth_mode_for_env(credential_env) == "api-key":
            lines = [
                f"export ANTHROPIC_API_KEY={shlex.quote(credential_env['ANTHROPIC_API_KEY'])}",
                *common,
            ]
        else:
            project = credential_env.get(
                "GOOGLE_CLOUD_PROJECT",
                credential_env.get(
                    "ANTHROPIC_VERTEX_PROJECT_ID", credential_env.get("GCP_PROJECT_ID", "")
                ),
            )
            location = credential_env.get(
                "VERTEX_LOCATION",
                credential_env.get("CLOUD_ML_REGION", "global"),
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
        if traceparent:
            lines.append(f"export TRACEPARENT={shlex.quote(traceparent)}")
        return lines

    def build_otel_exec_env(self, otel_port=None, traceparent=None):
        """Return OTel env vars for OpenCode.

        See docs/otel-configuration.md for why these differ from Claude Code.
        """
        if not otel_port:
            return []
        env = [
            "--env",
            f"OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{otel_port}",
            "--env",
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
            "--env",
            # Flush spans immediately -- OpenCode's process.exit() kills the
            # Node.js process before the batch processor can drain its queue.
            "OTEL_BSP_SCHEDULE_DELAY=0",
        ]
        if traceparent:
            env.extend(["--env", f"TRACEPARENT={traceparent}"])
        return env

    def build_local_env(self, otel_port=None, otel_rate_file=None, traceparent=None, env=None):
        credential_env = env if env is not None else os.environ
        env = {
            "AGENT_TOOL": "opencode",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            env["AGENT_ENABLED_PLUGINS"] = enabled_plugins
        if self.auth_mode_for_env(credential_env) == "api-key":
            env["ANTHROPIC_API_KEY"] = credential_env["ANTHROPIC_API_KEY"]
        else:
            env["GOOGLE_CLOUD_PROJECT"] = credential_env.get(
                "GOOGLE_CLOUD_PROJECT",
                credential_env.get(
                    "ANTHROPIC_VERTEX_PROJECT_ID", credential_env.get("GCP_PROJECT_ID", "")
                ),
            )
            env["VERTEX_LOCATION"] = credential_env.get(
                "VERTEX_LOCATION", credential_env.get("CLOUD_ML_REGION", "global")
            )
        if traceparent:
            env["TRACEPARENT"] = traceparent
        return env

    def credential_mount_target(self):
        return os.environ.get("OPENCODE_CONTAINER_HOME", "/home/agent-ci")

    def create_stream_processor(self, pid=0):
        return OpenCodeStreamProcessor(agent_pid=pid)

    def image_env_var(self):
        return "OPENCODE_CONTAINER_IMAGE"

    def model_env_var(self):
        return "OPENCODE_MODEL"

    def effort_env_var(self):
        return "OPENCODE_REASONING_EFFORT"

    def effort_args(self, effort, subagent_effort):
        return ["--variant", effort] if effort else []

    @property
    def supports_otel(self) -> bool:
        return True

    @property
    def autoupdater_env_var(self):
        return "OPENCODE_DISABLE_AUTOUPDATE"

    _CONTAINER_CONFIG_DIR = "/sandbox/.config/opencode"

    def write_sandbox_config(self, config_dir, otel_enabled=False):
        opencode_dir = os.path.join(config_dir, ".config", "opencode")
        os.makedirs(opencode_dir, exist_ok=True)
        config = {"$schema": "https://opencode.ai/config.json"}
        if otel_enabled:
            config["experimental"] = {"openTelemetry": True}
        with open(os.path.join(opencode_dir, "opencode.json"), "w") as f:
            json.dump(config, f, indent=2)

    def sandbox_config_mounts(self, config_dir):
        host_path = os.path.join(config_dir, ".config", "opencode", "opencode.json")
        if os.path.exists(host_path):
            return [(host_path, f"{self._CONTAINER_CONFIG_DIR}/opencode.json")]
        return []


class CodexHarness(Harness):
    """OpenAI Codex CLI harness."""

    registry_key = "codex"

    _CREDENTIAL_ENV_VARS = ("OPENAI_API_KEY",)

    @property
    def name(self) -> str:
        return "Codex"

    @property
    def auth_mode(self) -> str:
        """Codex uses OpenAI credentials, not Anthropic or Vertex."""
        return "openai"

    def auth_mode_for_env(self, env=None) -> str:
        """Codex always uses its OpenAI-compatible authentication path."""
        return "openai"

    def validate_credentials(
        self,
        env: Mapping[str, str] | None = None,
        *,
        allow_auth_file: bool = False,
    ) -> None:
        credential_env = env if env is not None else os.environ
        if any(credential_env.get(name) for name in self._CREDENTIAL_ENV_VARS):
            return

        home = Path(credential_env.get("HOME", str(Path.home())))
        codex_home = Path(credential_env.get("CODEX_HOME", str(home / ".codex")))
        auth_path = codex_home / "auth.json"
        if allow_auth_file and auth_path.is_file():
            return

        expected = ", ".join(self._CREDENTIAL_ENV_VARS)
        if allow_auth_file:
            expected = f"{expected}, or {auth_path}"
        raise RuntimeError(f"Codex credentials not found. Set one of: {expected}.")

    @staticmethod
    def _otel_config_args(endpoint):
        endpoint = endpoint.rstrip("/")

        def exporter(signal):
            url = json.dumps(f"{endpoint}/v1/{signal}")
            return f'{{ "otlp-http" = {{ endpoint = {url}, protocol = "json" }} }}'

        return [
            "-c",
            f"otel.exporter={exporter('logs')}",
            "-c",
            f"otel.metrics_exporter={exporter('metrics')}",
            "-c",
            f"otel.trace_exporter={exporter('traces')}",
        ]

    def build_args(
        self, prompt, model, extra_args=None, otel_endpoint=None, externally_sandboxed=False
    ):
        codex_args = [
            "exec",
            # Inside OpenShell the sandbox and its network policy already
            # isolate the agent, so skip Codex's own approval prompts and
            # workspace-write sandbox (with --approve-for-me its inner sandbox
            # blocked network and its auto-reviewer declined egress, so skills
            # could not post results). The plain runner image (podman/local)
            # has no such outer policy, so keep Codex's own safeguards there.
            "--dangerously-bypass-approvals-and-sandbox"
            if externally_sandboxed
            else "--approve-for-me",
            "--json",
            "--skip-git-repo-check",
            # Codex has no supported auto-update env var; use its native config.
            "-c",
            "check_for_update_on_startup=false",
        ]
        if otel_endpoint:
            codex_args.extend(self._otel_config_args(otel_endpoint))
        if extra_args:
            # Keep every extra argument before the model and prompt so Codex
            # can interpret subcommands/options such as ``resume --last``.
            codex_args.extend(arg for arg in extra_args if arg != "--")
        codex_args.extend(["-m", model, "--", prompt])
        return [
            "bash",
            "-c",
            'set -e; if [ -n "${OPENAI_API_KEY:-}" ]; then '
            'if ! printf "%s" "$OPENAI_API_KEY" | codex login --with-api-key >/dev/null 2>&1; then '
            '{ echo "codex login --with-api-key failed" >&2; exit 1; }; '
            "fi; "
            "fi; unset OPENAI_API_KEY; "
            'exec codex "$@"',
            "--",
            *codex_args,
        ]

    def build_env_args(self, env=None):
        credential_env = env if env is not None else os.environ
        args = ["--env", "AGENT_TOOL=codex"]
        if credential_env.get("OPENAI_API_KEY"):
            args.extend(["--env", "OPENAI_API_KEY"])
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            args.extend(["--env", f"AGENT_ENABLED_PLUGINS={enabled_plugins}"])
        return args

    def build_env_script_lines(
        self, otel_port=None, otel_rate_file=None, traceparent=None, env=None
    ):
        credential_env = env if env is not None else os.environ
        lines = [
            "mkdir -p /sandbox/.codex",
            "export AGENT_TOOL=codex",
            "export CODEX_HOME=/sandbox/.codex",
        ]
        if credential_env.get("OPENAI_API_KEY"):
            lines.append(f"export OPENAI_API_KEY={shlex.quote(credential_env['OPENAI_API_KEY'])}")
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            lines.append(f"export AGENT_ENABLED_PLUGINS={shlex.quote(enabled_plugins)}")
        # Codex parents its exec root span under TRACEPARENT when set
        # (codex_otel::traceparent_context_from_env), so its spans join the
        # agentic-ci root trace instead of starting a new one. The OTLP
        # exporters themselves are configured via -c flags in build_args.
        if traceparent:
            lines.append(f"export TRACEPARENT={shlex.quote(traceparent)}")
        return lines

    def build_otel_exec_env(self, otel_port=None, traceparent=None):
        if traceparent:
            return ["--env", f"TRACEPARENT={traceparent}"]
        return []

    def build_local_env(self, otel_port=None, otel_rate_file=None, traceparent=None, env=None):
        credential_env = env if env is not None else os.environ
        env = {"AGENT_TOOL": "codex"}
        enabled_plugins = credential_env.get("AGENT_ENABLED_PLUGINS")
        if enabled_plugins:
            env["AGENT_ENABLED_PLUGINS"] = enabled_plugins
        if traceparent:
            env["TRACEPARENT"] = traceparent
        return env

    def credential_mount_target(self):
        return os.environ.get("CODEX_CONTAINER_HOME", "/home/agent-ci")

    def create_stream_processor(self, pid=0):
        return CodexStreamProcessor(agent_pid=pid)

    def image_env_var(self):
        return "CODEX_CONTAINER_IMAGE"

    def model_env_var(self):
        return "CODEX_MODEL"

    def effort_env_var(self):
        return "CODEX_REASONING_EFFORT"

    def subagent_effort_env_var(self):
        return "CODEX_SUBAGENT_REASONING_EFFORT"

    def effort_args(self, effort, subagent_effort):
        args = []
        if effort:
            args.extend(["-c", f"model_reasoning_effort={effort}"])
        if subagent_effort:
            args.extend(["-c", f"agents.default_subagent_reasoning_effort={subagent_effort}"])
        return args

    @property
    def supports_otel(self) -> bool:
        return True


def create_harness(name: str) -> Harness:
    """Create a harness instance by name."""
    if name == "claude-code":
        return ClaudeCodeHarness()
    elif name == "opencode":
        return OpenCodeHarness()
    elif name == "codex":
        return CodexHarness()
    else:
        raise ValueError(
            f"Unknown harness: {name!r}. Choose 'claude-code', 'opencode', or 'codex'."
        )
