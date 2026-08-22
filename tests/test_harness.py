"""Tests for harness abstraction."""

import pytest

from agentic_ci.harness import (
    ClaudeCodeHarness,
    CodexHarness,
    OpenCodeHarness,
    create_harness,
)
from agentic_ci.stream import CodexStreamProcessor


def test_create_claude_code_harness():
    harness = create_harness("claude-code")
    assert isinstance(harness, ClaudeCodeHarness)


def test_create_opencode_harness():
    harness = create_harness("opencode")
    assert isinstance(harness, OpenCodeHarness)


def test_create_codex_harness():
    harness = create_harness("codex")
    assert isinstance(harness, CodexHarness)


def test_create_unknown_harness_raises():
    with pytest.raises(ValueError, match="Unknown harness"):
        create_harness("gemini")


class TestAuthMode:
    def test_vertex_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert ClaudeCodeHarness().auth_mode == "vertex"
        assert OpenCodeHarness().auth_mode == "vertex"

    def test_api_key_when_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert ClaudeCodeHarness().auth_mode == "api-key"
        assert OpenCodeHarness().auth_mode == "api-key"

    def test_vertex_when_api_key_empty(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert ClaudeCodeHarness().auth_mode == "vertex"
        assert OpenCodeHarness().auth_mode == "vertex"

    def test_api_key_from_explicit_environment(self):
        env = {"ANTHROPIC_API_KEY": "test-key"}
        assert ClaudeCodeHarness().auth_mode_for_env(env) == "api-key"
        assert OpenCodeHarness().auth_mode_for_env(env) == "api-key"


class TestClaudeCodeHarness:
    def test_name(self):
        assert ClaudeCodeHarness().name == "Claude Code"

    def test_build_args(self):
        harness = ClaudeCodeHarness()
        args = harness.build_args("do something", "claude-opus-4-6")
        assert args[0] == "claude"
        assert "--permission-mode" in args
        assert "bypassPermissions" in args
        assert "--output-format" in args
        assert "stream-json" in args
        assert "--model" in args
        assert "claude-opus-4-6" in args
        assert "-p" in args
        assert "do something" in args

    def test_build_args_with_extra(self):
        harness = ClaudeCodeHarness()
        args = harness.build_args("prompt", "model", extra_args=["--foo", "bar"])
        assert "--foo" in args
        assert "bar" in args

    def test_build_env_args(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east1")
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-proj")
        harness = ClaudeCodeHarness()
        args = harness.build_env_args()
        assert "CLAUDE_CODE_USE_VERTEX=1" in args
        assert "CLOUD_ML_REGION=us-east1" in args
        assert "ANTHROPIC_VERTEX_PROJECT_ID=my-proj" in args
        assert "DISABLE_AUTOUPDATER=1" in args

    def test_build_env_args_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        harness = ClaudeCodeHarness()
        args = harness.build_env_args()
        assert "ANTHROPIC_API_KEY" in args
        assert "ANTHROPIC_API_KEY=sk-test-key" not in args
        assert "DISABLE_AUTOUPDATER=1" in args
        assert "CLAUDE_CODE_USE_VERTEX=1" not in args

    def test_build_env_args_gcp_project_id_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.setenv("GCP_PROJECT_ID", "gcp-proj")
        harness = ClaudeCodeHarness()
        args = harness.build_env_args()
        assert "ANTHROPIC_VERTEX_PROJECT_ID=gcp-proj" in args

    def test_build_env_script_lines_gcp_project_id_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.setenv("GCP_PROJECT_ID", "gcp-proj")
        harness = ClaudeCodeHarness()
        lines = harness.build_env_script_lines()
        assert any("ANTHROPIC_VERTEX_PROJECT_ID=gcp-proj" in line for line in lines)

    def test_build_otel_exec_env(self):
        harness = ClaudeCodeHarness()
        args = harness.build_otel_exec_env(otel_port=4318)
        assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in args
        assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318" in args

    def test_build_otel_exec_env_empty_without_port(self):
        assert ClaudeCodeHarness().build_otel_exec_env(otel_port=None) == []

    def test_build_local_env_vertex(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east1")
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-proj")
        env = ClaudeCodeHarness().build_local_env()
        assert env["AGENT_TOOL"] == "claude"
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["CLAUDE_CODE_ENTRYPOINT"] == "sdk-cli"
        assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert env["CLOUD_ML_REGION"] == "us-east1"
        assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "my-proj"
        assert "ANTHROPIC_API_KEY" not in env

    def test_build_local_env_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        env = ClaudeCodeHarness().build_local_env()
        assert env["ANTHROPIC_API_KEY"] == "sk-test-key"
        assert "CLAUDE_CODE_USE_VERTEX" not in env

    def test_build_local_env_gcp_project_id_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.setenv("GCP_PROJECT_ID", "gcp-proj")
        env = ClaudeCodeHarness().build_local_env()
        assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "gcp-proj"

    def test_build_local_env_with_otel(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        env = ClaudeCodeHarness().build_local_env(otel_port=4318)
        assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"
        assert env["OTEL_TRACES_EXPORTER"] == "otlp"

    def test_build_local_env_no_otel_without_port(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        env = ClaudeCodeHarness().build_local_env()
        assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in env
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env

    def test_build_env_script_lines(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-west1")
        harness = ClaudeCodeHarness()
        lines = harness.build_env_script_lines()
        assert any("CLAUDE_CODE_USE_VERTEX=1" in line for line in lines)
        assert any("DISABLE_AUTOUPDATER=1" in line for line in lines)

    def test_build_env_script_lines_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        harness = ClaudeCodeHarness()
        lines = harness.build_env_script_lines()
        assert "export ANTHROPIC_API_KEY=sk-test-key" in lines
        assert any("DISABLE_AUTOUPDATER=1" in line for line in lines)
        assert any("CLAUDE_CODE_PLUGIN_SEED_DIR=/sandbox/.claude-seed" in line for line in lines)
        assert not any("CLAUDE_CODE_USE_VERTEX" in line for line in lines)
        assert not any("GOOGLE_APPLICATION_CREDENTIALS" in line for line in lines)

    def test_build_env_script_lines_forwards_enabled_plugins(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha,beta")
        harness = ClaudeCodeHarness()
        lines = harness.build_env_script_lines()
        assert any("AGENT_ENABLED_PLUGINS" in line for line in lines)
        assert any("alpha,beta" in line for line in lines)

    def test_build_env_script_lines_no_enabled_plugins_when_unset(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("AGENT_ENABLED_PLUGINS", raising=False)
        harness = ClaudeCodeHarness()
        lines = harness.build_env_script_lines()
        assert not any("AGENT_ENABLED_PLUGINS" in line for line in lines)

    def test_build_env_script_lines_with_otel(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
        harness = ClaudeCodeHarness()
        lines = harness.build_env_script_lines(otel_port=4318)
        assert any("CLAUDE_CODE_ENABLE_TELEMETRY=1" in line for line in lines)
        assert any("OTEL_EXPORTER_OTLP_ENDPOINT=http://10.200.0.1:4318" in line for line in lines)
        assert not any("OTEL_RATE_FILE" in line for line in lines)

    def test_credential_mount_target(self):
        assert ClaudeCodeHarness().credential_mount_target() == "/home/agent-ci"

    def test_credential_mount_target_env_override(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONTAINER_HOME", "/home/claude")
        assert ClaudeCodeHarness().credential_mount_target() == "/home/claude"

    def test_create_stream_processor(self):
        from agentic_ci.stream import ClaudeCodeStreamProcessor

        proc = ClaudeCodeHarness().create_stream_processor(pid=123)
        assert isinstance(proc, ClaudeCodeStreamProcessor)

    def test_image_env_var(self):
        assert ClaudeCodeHarness().image_env_var() == "CLAUDE_CONTAINER_IMAGE"

    def test_model_env_var(self):
        assert ClaudeCodeHarness().model_env_var() == "CLAUDE_MODEL"

    def test_default_model(self):
        assert ClaudeCodeHarness().default_model() == "claude-opus-4-6"


class TestOpenCodeHarness:
    def test_name(self):
        assert OpenCodeHarness().name == "OpenCode"

    def test_build_args(self):
        harness = OpenCodeHarness()
        args = harness.build_args("do something", "google-vertex/claude-haiku-4-5@20251001")
        assert args[0] == "opencode"
        assert "run" in args
        assert "--format" in args
        assert "json" in args
        assert "--dangerously-skip-permissions" in args
        assert "-m" in args
        assert "google-vertex/claude-haiku-4-5@20251001" in args
        assert "do something" in args

    def test_build_args_with_extra(self):
        harness = OpenCodeHarness()
        args = harness.build_args("prompt", "model", extra_args=["--thinking"])
        assert "--thinking" in args

    def test_build_env_args(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
        monkeypatch.setenv("VERTEX_LOCATION", "us-central1")
        harness = OpenCodeHarness()
        args = harness.build_env_args()
        assert "GOOGLE_CLOUD_PROJECT=my-proj" in args
        assert "VERTEX_LOCATION=us-central1" in args
        assert "OPENCODE_DISABLE_AUTOUPDATE=1" in args

    def test_build_env_args_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        harness = OpenCodeHarness()
        args = harness.build_env_args()
        assert "ANTHROPIC_API_KEY" in args
        assert "ANTHROPIC_API_KEY=sk-test-key" not in args
        assert "OPENCODE_DISABLE_AUTOUPDATE=1" in args
        assert not any("GOOGLE_CLOUD_PROJECT" in a for a in args)

    def test_build_env_args_fallback(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("VERTEX_LOCATION", raising=False)
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "fallback-proj")
        monkeypatch.setenv("CLOUD_ML_REGION", "eu-west1")
        harness = OpenCodeHarness()
        args = harness.build_env_args()
        assert "GOOGLE_CLOUD_PROJECT=fallback-proj" in args
        assert "VERTEX_LOCATION=eu-west1" in args

    def test_build_env_args_gcp_project_id_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("VERTEX_LOCATION", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        monkeypatch.setenv("GCP_PROJECT_ID", "gcp-proj")
        harness = OpenCodeHarness()
        args = harness.build_env_args()
        assert "GOOGLE_CLOUD_PROJECT=gcp-proj" in args

    def test_build_env_script_lines_gcp_project_id_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("VERTEX_LOCATION", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        monkeypatch.setenv("GCP_PROJECT_ID", "gcp-proj")
        harness = OpenCodeHarness()
        lines = harness.build_env_script_lines()
        assert any("GOOGLE_CLOUD_PROJECT=gcp-proj" in line for line in lines)

    def test_build_otel_exec_env(self):
        env = OpenCodeHarness().build_otel_exec_env(otel_port=4318)
        assert "--env" in env
        assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318" in env
        assert "OTEL_EXPORTER_OTLP_PROTOCOL=http/json" in env
        assert "OTEL_BSP_SCHEDULE_DELAY=0" in env

    def test_build_otel_exec_env_none_port(self):
        assert OpenCodeHarness().build_otel_exec_env(otel_port=None) == []

    def test_build_local_env_vertex(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
        monkeypatch.setenv("VERTEX_LOCATION", "us-central1")
        env = OpenCodeHarness().build_local_env()
        assert env["AGENT_TOOL"] == "opencode"
        assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
        assert env["GOOGLE_CLOUD_PROJECT"] == "my-proj"
        assert env["VERTEX_LOCATION"] == "us-central1"
        assert "ANTHROPIC_API_KEY" not in env

    def test_build_local_env_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        env = OpenCodeHarness().build_local_env()
        assert env["ANTHROPIC_API_KEY"] == "sk-test-key"
        assert "GOOGLE_CLOUD_PROJECT" not in env

    def test_build_local_env_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("VERTEX_LOCATION", raising=False)
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "fallback-proj")
        monkeypatch.setenv("CLOUD_ML_REGION", "eu-west1")
        env = OpenCodeHarness().build_local_env()
        assert env["GOOGLE_CLOUD_PROJECT"] == "fallback-proj"
        assert env["VERTEX_LOCATION"] == "eu-west1"

    def test_build_local_env_gcp_project_id_fallback(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("VERTEX_LOCATION", raising=False)
        monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
        monkeypatch.setenv("GCP_PROJECT_ID", "gcp-proj")
        env = OpenCodeHarness().build_local_env()
        assert env["GOOGLE_CLOUD_PROJECT"] == "gcp-proj"

    def test_build_local_env_no_otel(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        env = OpenCodeHarness().build_local_env(otel_port=4318)
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env

    def test_build_env_script_lines(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        monkeypatch.setenv("VERTEX_LOCATION", "global")
        harness = OpenCodeHarness()
        lines = harness.build_env_script_lines()
        assert any("GOOGLE_CLOUD_PROJECT=" in line for line in lines)
        assert any("OPENCODE_DISABLE_AUTOUPDATE=1" in line for line in lines)

    def test_build_env_script_lines_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        harness = OpenCodeHarness()
        lines = harness.build_env_script_lines()
        assert "export ANTHROPIC_API_KEY=sk-test-key" in lines

    def test_build_env_script_lines_forwards_enabled_plugins(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha,beta")
        harness = OpenCodeHarness()
        lines = harness.build_env_script_lines()
        assert any("AGENT_ENABLED_PLUGINS" in line for line in lines)

    def test_build_env_script_lines_no_enabled_plugins_when_unset(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("AGENT_ENABLED_PLUGINS", raising=False)
        harness = OpenCodeHarness()
        lines = harness.build_env_script_lines()
        assert not any("AGENT_ENABLED_PLUGINS" in line for line in lines)
        assert any("OPENCODE_DISABLE_AUTOUPDATE=1" in line for line in lines)
        assert not any("GOOGLE_CLOUD_PROJECT" in line for line in lines)
        assert not any("GOOGLE_APPLICATION_CREDENTIALS" in line for line in lines)

    def test_credential_mount_target(self):
        assert OpenCodeHarness().credential_mount_target() == "/home/agent-ci"

    def test_credential_mount_target_env_override(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_CONTAINER_HOME", "/home/opencode")
        assert OpenCodeHarness().credential_mount_target() == "/home/opencode"

    def test_create_stream_processor(self):
        from agentic_ci.stream import OpenCodeStreamProcessor

        proc = OpenCodeHarness().create_stream_processor(pid=456)
        assert isinstance(proc, OpenCodeStreamProcessor)

    def test_image_env_var(self):
        assert OpenCodeHarness().image_env_var() == "OPENCODE_CONTAINER_IMAGE"

    def test_model_env_var(self):
        assert OpenCodeHarness().model_env_var() == "OPENCODE_MODEL"

    def test_default_model(self):
        assert OpenCodeHarness().default_model() == "google-vertex/claude-opus-4-6@default"

    def test_build_env_script_lines_with_otel(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        harness = OpenCodeHarness()
        lines = harness.build_env_script_lines(otel_port=4318)
        assert any("OTEL_EXPORTER_OTLP_ENDPOINT=" in line for line in lines)
        assert any("OTEL_EXPORTER_OTLP_PROTOCOL=http/json" in line for line in lines)
        assert any("OTEL_BSP_SCHEDULE_DELAY=0" in line for line in lines)

    def test_write_sandbox_config_otel_enabled(self, tmp_path):
        harness = OpenCodeHarness()
        harness.write_sandbox_config(str(tmp_path), otel_enabled=True)
        config_file = tmp_path / ".config" / "opencode" / "opencode.json"
        assert config_file.exists()
        import json

        config = json.loads(config_file.read_text())
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert config["experimental"]["openTelemetry"] is True

    def test_write_sandbox_config_otel_disabled(self, tmp_path):
        harness = OpenCodeHarness()
        harness.write_sandbox_config(str(tmp_path), otel_enabled=False)
        config_file = tmp_path / ".config" / "opencode" / "opencode.json"
        assert config_file.exists()
        import json

        config = json.loads(config_file.read_text())
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert "experimental" not in config

    def test_sandbox_config_mounts_with_config(self, tmp_path):
        harness = OpenCodeHarness()
        harness.write_sandbox_config(str(tmp_path), otel_enabled=True)
        mounts = harness.sandbox_config_mounts(str(tmp_path))
        assert len(mounts) == 1
        host_path, container_path = mounts[0]
        assert host_path.endswith("opencode.json")
        assert container_path == "/sandbox/.config/opencode/opencode.json"

    def test_sandbox_config_mounts_without_config(self, tmp_path):
        harness = OpenCodeHarness()
        mounts = harness.sandbox_config_mounts(str(tmp_path))
        assert mounts == []


class TestCodexHarness:
    def test_name(self):
        assert CodexHarness().name == "Codex"

    def test_auth_mode_is_openai(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert CodexHarness().auth_mode == "openai"

    def test_build_args(self):
        harness = CodexHarness()
        args = harness.build_args("do something", "gpt-5.6-sol")
        assert args[0:2] == ["bash", "-c"]
        assert "codex login --with-api-key" in args[2]
        assert "codex login --with-api-key failed" in args[2]
        assert "2>&1" not in args[2]
        assert 'exec codex "$@"' in args[2]
        assert "exec" in args
        assert "--dangerously-bypass-approvals-and-sandbox" in args
        assert "--json" in args
        assert "--skip-git-repo-check" in args
        assert "--ephemeral" in args
        assert "--ignore-user-config" not in args
        config_values = [args[index + 1] for index, arg in enumerate(args) if arg == "-c"]
        assert "check_for_update_on_startup=false" in config_values
        assert "-m" in args
        assert "gpt-5.6-sol" in args
        assert "do something" in args

    def test_build_args_with_otel(self):
        args = CodexHarness().build_args(
            "prompt",
            "model",
            otel_endpoint="http://127.0.0.1:4318",
        )
        config_values = [args[index + 1] for index, arg in enumerate(args) if arg == "-c"]
        assert any("http://127.0.0.1:4318/v1/logs" in value for value in config_values)
        assert any("http://127.0.0.1:4318/v1/metrics" in value for value in config_values)
        assert any("http://127.0.0.1:4318/v1/traces" in value for value in config_values)

    def test_build_args_with_extra(self):
        harness = CodexHarness()
        args = harness.build_args("prompt", "model", extra_args=["--foo", "bar"])
        assert "--foo" in args
        assert "bar" in args

    def test_build_args_keeps_prompt_after_option_delimiter(self):
        args = CodexHarness().build_args("--help", "model", extra_args=["--color", "never"])

        assert args[-1] == "--help"
        assert args[-2] == "--"
        assert args.index("--color") < len(args) - 2

    def test_build_env_args(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        harness = CodexHarness()
        args = harness.build_env_args()
        assert "OPENAI_API_KEY" in args
        assert "AGENT_TOOL=codex" in args
        assert not any("test-key" in arg for arg in args)

    def test_build_env_script_lines(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        harness = CodexHarness()
        lines = harness.build_env_script_lines()
        assert "export OPENAI_API_KEY=sk-test-key" in lines
        assert "mkdir -p /sandbox/.codex" in lines
        assert any("AGENT_TOOL=codex" in line for line in lines)
        assert any("CODEX_HOME=/sandbox/.codex" in line for line in lines)

    def test_build_env_script_lines_without_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        lines = CodexHarness().build_env_script_lines()

        assert not any("OPENAI_API_KEY" in line for line in lines)

    def test_build_env_script_lines_forwards_enabled_plugins(self, monkeypatch):
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha,beta")
        lines = CodexHarness().build_env_script_lines()
        assert any("AGENT_ENABLED_PLUGINS=alpha,beta" in line for line in lines)

    def test_build_otel_exec_env_always_empty(self):
        assert CodexHarness().build_otel_exec_env(otel_port=4318) == []

    def test_build_local_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha")
        env = CodexHarness().build_local_env()
        assert env["AGENT_TOOL"] == "codex"
        assert env["AGENT_ENABLED_PLUGINS"] == "alpha"

    def test_build_local_env_does_not_copy_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key")

        env = CodexHarness().build_local_env()

        assert "OPENAI_API_KEY" not in env

    def test_credential_mount_target(self):
        assert CodexHarness().credential_mount_target() == "/home/agent-ci"

    def test_credential_mount_target_env_override(self, monkeypatch):
        monkeypatch.setenv("CODEX_CONTAINER_HOME", "/home/codex")
        assert CodexHarness().credential_mount_target() == "/home/codex"

    def test_create_stream_processor(self):
        proc = CodexHarness().create_stream_processor(pid=789)
        assert isinstance(proc, CodexStreamProcessor)

    def test_image_env_var(self):
        assert CodexHarness().image_env_var() == "CODEX_CONTAINER_IMAGE"

    def test_model_env_var(self):
        assert CodexHarness().model_env_var() == "CODEX_MODEL"

    def test_default_model(self):
        assert CodexHarness().default_model() == "gpt-5.6-sol"

    def test_supports_otel(self):
        assert CodexHarness().supports_otel is True
