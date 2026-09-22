"""Tests for harness abstraction."""

import json
import os
import subprocess

import pytest

from agentic_ci.harness import (
    ClaudeCodeHarness,
    CodexHarness,
    OpenCodeHarness,
    create_harness,
    vertex_base_url,
)
from agentic_ci.routing import ModelTier
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
        # OpenShell placeholder-token auth: no ADC or metadata server in the sandbox.
        assert "export CLAUDE_CODE_SKIP_VERTEX_AUTH=1" in lines
        assert (
            'export ANTHROPIC_AUTH_TOKEN="${GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN:-'
            '${GOOGLE_VERTEX_AI_TOKEN:-}}"'
        ) in lines
        assert (
            "export ANTHROPIC_VERTEX_BASE_URL=https://us-west1-aiplatform.googleapis.com/v1"
            in lines
        )

    @pytest.mark.parametrize(
        ("region", "url"),
        [
            ("global", "https://aiplatform.googleapis.com/v1"),
            ("us", "https://aiplatform.us.rep.googleapis.com/v1"),
            ("eu", "https://aiplatform.eu.rep.googleapis.com/v1"),
            ("us-east5", "https://us-east5-aiplatform.googleapis.com/v1"),
        ],
    )
    def test_vertex_base_url_matches_claude_code_host_selection(self, region, url):
        assert vertex_base_url(region) == url

    def test_build_env_args_does_not_skip_vertex_auth(self, monkeypatch):
        # The podman backend mounts real ADC; only the OpenShell env script
        # switches Claude Code to placeholder bearer auth.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
        args = ClaudeCodeHarness().build_env_args()
        assert not any("SKIP_VERTEX_AUTH" in a for a in args)
        assert not any("ANTHROPIC_AUTH_TOKEN" in a for a in args)

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

    def test_default_model_tiers(self):
        harness = ClaudeCodeHarness()
        tiers = harness.default_model_tiers()
        assert set(tiers) == {"low", "medium", "high"}
        assert tiers["high"].model == harness.default_model()
        assert tiers["low"] == ModelTier("claude-sonnet-4-5", "medium")
        assert tiers["medium"] == ModelTier("claude-sonnet-4-5", "high")
        assert tiers["high"].effort == "high"

    def test_build_effort_args_none_is_empty(self):
        assert ClaudeCodeHarness().build_effort_args(None) == []

    def test_build_effort_args(self):
        assert ClaudeCodeHarness().build_effort_args("xhigh") == ["--effort", "xhigh"]

    def test_effort_env_var(self):
        assert ClaudeCodeHarness().effort_env_var() == "CLAUDE_REASONING_EFFORT"
        assert ClaudeCodeHarness().subagent_effort_env_var() is None

    def test_resolve_efforts_default_is_high(self):
        assert ClaudeCodeHarness().resolve_efforts(env={}) == ("high", None)

    def test_resolve_efforts_env_override(self):
        env = {"CLAUDE_REASONING_EFFORT": "max"}
        assert ClaudeCodeHarness().resolve_efforts(env=env) == ("max", None)

    def test_resolve_efforts_explicit_beats_env(self):
        env = {"CLAUDE_REASONING_EFFORT": "max"}
        assert ClaudeCodeHarness().resolve_efforts("low", env=env) == ("low", None)

    def test_resolve_efforts_none_disables_flag(self):
        harness = ClaudeCodeHarness()
        assert harness.resolve_efforts(env={"CLAUDE_REASONING_EFFORT": "none"}) == (None, None)
        assert harness.build_effort_args(None, None) == []

    def test_invalid_env_effort_fails_before_run(self):
        harness = ClaudeCodeHarness()
        effort, sub = harness.resolve_efforts(env={"CLAUDE_REASONING_EFFORT": "bogus"})
        with pytest.raises(ValueError, match="Unsupported Claude Code effort 'bogus'"):
            harness.build_effort_args(effort, sub)

    def test_build_effort_args_invalid_raises(self):
        with pytest.raises(ValueError, match="Unsupported Claude Code effort"):
            ClaudeCodeHarness().build_effort_args("turbo")

    def test_classifier_effort(self):
        assert ClaudeCodeHarness().classifier_effort() == "low"

    def test_build_classifier_args(self):
        assert ClaudeCodeHarness().build_classifier_args(7) == ["--max-turns", "7"]

    def test_effort_args_appended_after_prompt(self):
        harness = ClaudeCodeHarness()
        args = harness.build_args("prompt", "model", extra_args=harness.build_effort_args("high"))
        assert args[-2:] == ["--effort", "high"]


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

    def test_build_env_script_lines_vertex_uses_token_stub_adc(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
        lines = OpenCodeHarness().build_env_script_lines()
        joined = "\n".join(lines)
        assert (
            'export AGENTIC_CI_VERTEX_TOKEN="${GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN:-'
            '${GOOGLE_VERTEX_AI_TOKEN:-}}"'
        ) in lines
        assert "export GOOGLE_APPLICATION_CREDENTIALS=/tmp/.agentic-ci-vertex-adc.json" in lines
        assert "export METADATA_SERVER_DETECTION=none" in lines
        assert "agentic-ci vertex-token-stub --port 8175" in joined
        assert '"token_url":"http://127.0.0.1:8175/token"' in joined
        assert '"type":"external_account"' in joined
        # The stub must be running before GOOGLE_APPLICATION_CREDENTIALS points at it.
        stub_index = next(i for i, line in enumerate(lines) if "vertex-token-stub" in line)
        adc_index = lines.index(
            "export GOOGLE_APPLICATION_CREDENTIALS=/tmp/.agentic-ci-vertex-adc.json"
        )
        assert stub_index < adc_index

    def test_build_env_script_lines_api_key_has_no_token_stub(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        lines = OpenCodeHarness().build_env_script_lines()
        assert not any("vertex-token-stub" in line for line in lines)
        assert not any("GOOGLE_APPLICATION_CREDENTIALS" in line for line in lines)

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

    def test_default_model_tiers(self):
        harness = OpenCodeHarness()
        tiers = harness.default_model_tiers()
        assert set(tiers) == {"low", "medium", "high"}
        assert tiers["high"].model == harness.default_model()
        assert tiers["low"] == ModelTier("google-vertex/claude-sonnet-4-5@20250929", None)
        assert tiers["medium"] == ModelTier("google-vertex/claude-sonnet-4-5@20250929", "high")
        assert tiers["high"].effort == "high"

    def test_build_effort_args_none_is_empty(self):
        assert OpenCodeHarness().build_effort_args(None) == []

    def test_build_effort_args(self):
        assert OpenCodeHarness().build_effort_args("max") == ["--variant", "max"]

    def test_effort_env_var(self):
        assert OpenCodeHarness().effort_env_var() == "OPENCODE_REASONING_EFFORT"
        assert OpenCodeHarness().subagent_effort_env_var() is None

    def test_resolve_efforts_default_is_high(self):
        assert OpenCodeHarness().resolve_efforts(env={}) == ("high", None)

    def test_resolve_efforts_env_override(self):
        env = {"OPENCODE_REASONING_EFFORT": "max"}
        assert OpenCodeHarness().resolve_efforts(env=env) == ("max", None)

    def test_resolve_efforts_none_disables_flag(self):
        harness = OpenCodeHarness()
        assert harness.resolve_efforts(env={"OPENCODE_REASONING_EFFORT": "none"}) == (None, None)

    def test_build_effort_args_invalid_raises(self):
        with pytest.raises(ValueError, match="Unsupported OpenCode effort"):
            OpenCodeHarness().build_effort_args("xhigh")

    def test_classifier_effort(self):
        assert OpenCodeHarness().classifier_effort() is None

    def test_build_classifier_args_has_no_turn_cap(self):
        assert OpenCodeHarness().build_classifier_args(7) == []

    def test_effort_args_appended_after_prompt(self):
        harness = OpenCodeHarness()
        args = harness.build_args("prompt", "model", extra_args=harness.build_effort_args("high"))
        assert args[-2:] == ["--variant", "high"]

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
        args = harness.build_args("do something", "gpt-6-sol")
        assert args[0:2] == ["bash", "-c"]
        assert "codex login --with-api-key" in args[2]
        assert "codex login --with-api-key failed" in args[2]
        assert ">/dev/null 2>&1" in args[2]
        assert "unset OPENAI_API_KEY" in args[2]
        assert 'exec codex "$@"' in args[2]
        assert "exec" in args
        assert "--approve-for-me" in args
        assert "--dangerously-bypass-approvals-and-sandbox" not in args
        assert "--json" in args
        assert "--skip-git-repo-check" in args
        assert "--ephemeral" not in args
        assert "--ignore-user-config" not in args
        config_values = [args[index + 1] for index, arg in enumerate(args) if arg == "-c"]
        assert "check_for_update_on_startup=false" in config_values
        assert "-m" in args
        assert "gpt-6-sol" in args
        assert "do something" in args

    def test_build_env_script_lines_exports_traceparent(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AGENT_ENABLED_PLUGINS", raising=False)
        lines = CodexHarness().build_env_script_lines(traceparent="00-abc-def-01")
        assert "export TRACEPARENT=00-abc-def-01" in lines

    def test_build_env_script_lines_without_traceparent(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("AGENT_ENABLED_PLUGINS", raising=False)
        lines = CodexHarness().build_env_script_lines()
        assert not any(line.startswith("export TRACEPARENT=") for line in lines)

    def test_build_otel_exec_env_forwards_traceparent(self):
        assert CodexHarness().build_otel_exec_env(otel_port=4318, traceparent="00-abc-def-01") == [
            "--env",
            "TRACEPARENT=00-abc-def-01",
        ]
        assert CodexHarness().build_otel_exec_env(otel_port=4318) == []

    def test_build_local_env_forwards_traceparent(self, monkeypatch):
        monkeypatch.delenv("AGENT_ENABLED_PLUGINS", raising=False)
        env = CodexHarness().build_local_env(traceparent="00-abc-def-01")
        assert env["TRACEPARENT"] == "00-abc-def-01"
        assert "TRACEPARENT" not in CodexHarness().build_local_env()

    def test_build_args_externally_sandboxed_bypasses_inner_sandbox(self):
        args = CodexHarness().build_args("do something", "gpt-6-sol", externally_sandboxed=True)
        assert "--dangerously-bypass-approvals-and-sandbox" in args
        assert "--approve-for-me" not in args

    def test_build_args_default_keeps_codex_safeguards(self):
        args = CodexHarness().build_args("do something", "gpt-6-sol")
        assert "--approve-for-me" in args
        assert "--dangerously-bypass-approvals-and-sandbox" not in args

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
        model_index = args.index("-m")
        assert args[model_index - 2 : model_index] == ["--foo", "bar"]

    def test_build_args_with_resume_keeps_options_before_model_and_prompt(self):
        args = CodexHarness().build_args(
            "follow-up prompt", "model", extra_args=["resume", "--last"]
        )

        model_index = args.index("-m")
        delimiter_index = args.index("--", model_index)
        prompt_index = args.index("follow-up prompt")
        assert args[args.index("resume") : model_index] == ["resume", "--last"]
        assert model_index < delimiter_index < prompt_index
        assert args[delimiter_index : prompt_index + 1] == ["--", "follow-up prompt"]

    def test_build_args_without_resume_keeps_normal_command_valid(self):
        args = CodexHarness().build_args("normal prompt", "model")

        assert args[-4:] == ["-m", "model", "--", "normal prompt"]

    def test_build_args_keeps_prompt_after_option_delimiter(self):
        args = CodexHarness().build_args("--help", "model", extra_args=["--color", "never"])

        assert args[-1] == "--help"
        assert args[-2] == "--"
        assert args.index("--color") < len(args) - 2

    def test_api_key_login_is_silent_and_unsets_key_before_jsonl_process(self, tmp_path):
        fake_codex = tmp_path / "codex"
        fake_codex.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = login ]; then\n'
            "  printf 'login stdout\\n'\n"
            "  printf 'login stderr\\n' >&2\n"
            "  cat >/dev/null\n"
            "  exit 0\n"
            "fi\n"
            'if [ -n "${OPENAI_API_KEY+x}" ]; then\n'
            "  printf '{\"api_key_unset\":false}\\n'\n"
            "else\n"
            "  printf '{\"api_key_unset\":true}\\n'\n"
            "fi\n"
        )
        fake_codex.chmod(0o755)

        secret = "sk-test-secret"
        env = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "OPENAI_API_KEY": secret,
        }
        completed = subprocess.run(
            CodexHarness().build_args("prompt", "model"),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )

        assert [json.loads(line) for line in completed.stdout.splitlines()] == [
            {"api_key_unset": True}
        ]
        assert completed.stderr == ""
        assert secret not in completed.stdout
        assert secret not in completed.stderr

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
        assert CodexHarness().default_model() == "gpt-6-sol"

    def test_default_model_tiers(self):
        harness = CodexHarness()
        tiers = harness.default_model_tiers()
        assert set(tiers) == {"low", "medium", "high"}
        assert tiers["high"].model == harness.default_model()
        assert tiers["low"] == ModelTier("gpt-6-luna", "xhigh")
        assert tiers["medium"] == ModelTier("gpt-6-luna", "xhigh")
        assert tiers["high"].effort == "high"

    def test_build_effort_args_none_is_empty(self):
        assert CodexHarness().build_effort_args(None) == []

    def test_build_effort_args(self):
        assert CodexHarness().build_effort_args("low") == ["-c", "model_reasoning_effort=low"]

    def test_build_effort_args_with_subagent(self):
        assert CodexHarness().build_effort_args("high", "medium") == [
            "-c",
            "model_reasoning_effort=high",
            "-c",
            "agents.default_subagent_reasoning_effort=medium",
        ]

    def test_effort_env_vars(self):
        assert CodexHarness().effort_env_var() == "CODEX_REASONING_EFFORT"
        assert CodexHarness().subagent_effort_env_var() == "CODEX_SUBAGENT_REASONING_EFFORT"

    def test_resolve_efforts_default_is_high_for_main_and_subagents(self):
        assert CodexHarness().resolve_efforts(env={}) == ("high", "high")

    def test_resolve_efforts_subagent_follows_main_override(self):
        env = {"CODEX_REASONING_EFFORT": "medium"}
        assert CodexHarness().resolve_efforts(env=env) == ("medium", "medium")

    def test_resolve_efforts_subagent_env_override(self):
        env = {"CODEX_REASONING_EFFORT": "high", "CODEX_SUBAGENT_REASONING_EFFORT": "low"}
        assert CodexHarness().resolve_efforts(env=env) == ("high", "low")

    def test_resolve_efforts_none_disables_both(self):
        assert CodexHarness().resolve_efforts(env={"CODEX_REASONING_EFFORT": "none"}) == (
            None,
            None,
        )

    def test_invalid_subagent_effort_fails_before_run(self):
        harness = CodexHarness()
        effort, sub = harness.resolve_efforts(env={"CODEX_SUBAGENT_REASONING_EFFORT": "bogus"})
        with pytest.raises(ValueError, match="Unsupported Codex sub-agent effort 'bogus'"):
            harness.build_effort_args(effort, sub)

    def test_default_run_sets_both_efforts_in_args(self):
        harness = CodexHarness()
        effort, sub = harness.resolve_efforts(env={})
        args = harness.build_args(
            "prompt", "model", extra_args=harness.build_effort_args(effort, sub)
        )
        assert "model_reasoning_effort=high" in args
        assert "agents.default_subagent_reasoning_effort=high" in args
        assert args[-4:] == ["-m", "model", "--", "prompt"]

    def test_build_effort_args_invalid_raises(self):
        with pytest.raises(ValueError, match="Unsupported Codex effort"):
            CodexHarness().build_effort_args("max")

    def test_classifier_effort(self):
        assert CodexHarness().classifier_effort() == "low"

    def test_build_classifier_args_has_no_turn_cap(self):
        assert CodexHarness().build_classifier_args(7) == []

    def test_effort_args_stay_before_model_and_prompt(self):
        harness = CodexHarness()
        args = harness.build_args("prompt", "model", extra_args=harness.build_effort_args("low"))
        assert args[-4:] == ["-m", "model", "--", "prompt"]
        effort_index = args.index("model_reasoning_effort=low")
        assert args[effort_index - 1] == "-c"
        assert effort_index < args.index("-m")

    def test_supports_otel(self):
        assert CodexHarness().supports_otel is True
