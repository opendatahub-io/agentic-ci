"""Tests for backend factory."""

import json
import threading
from unittest import mock

import pytest

from agentic_ci.backends import create_backend
from agentic_ci.backends.local import LocalBackend
from agentic_ci.backends.openshell import (
    OpenShellBackend,
    _load_sandbox_identity,
    _token_keepalive,
)
from agentic_ci.backends.podman import PodmanBackend
from agentic_ci.harness import ClaudeCodeHarness, CodexHarness, create_harness


@pytest.fixture()
def harness():
    return create_harness("claude-code")


def test_create_podman_backend(harness):
    backend = create_backend("podman", harness=harness, workdir="/tmp")
    assert isinstance(backend, PodmanBackend)
    assert backend.workdir == "/tmp"
    assert backend.harness is harness


def test_create_openshell_backend(harness):
    backend = create_backend("openshell", harness=harness, workdir="/tmp")
    assert isinstance(backend, OpenShellBackend)
    assert backend.workdir == "/tmp"
    assert backend.harness is harness


def test_create_local_backend(harness):
    backend = create_backend("local", harness=harness, workdir="/tmp")
    assert isinstance(backend, LocalBackend)
    assert backend.workdir == "/tmp"
    assert backend.harness is harness
    assert backend.image is None


def test_create_local_with_extra_env(harness):
    backend = create_backend("local", harness=harness, extra_env={"FOO": "bar"})
    assert backend._extra_env == {"FOO": "bar"}


def test_create_podman_with_image(harness):
    backend = create_backend("podman", harness=harness, image="my-image:latest")
    assert backend.image == "my-image:latest"


def test_create_openshell_with_policy(harness):
    backend = create_backend("openshell", harness=harness, policy="/path/to/policy.yml")
    assert backend.policy_path == "/path/to/policy.yml"


def test_create_podman_with_timeout(harness):
    backend = create_backend("podman", harness=harness, timeout=600)
    assert backend.timeout == 600


def test_load_sandbox_identity_handles_invalid_utf8(monkeypatch, tmp_path):
    state_path = tmp_path / "openshell-state.json"
    state_path.write_bytes(b"\xff")
    monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state_path))

    assert _load_sandbox_identity() is None


def test_create_podman_with_extra_env(harness):
    backend = create_backend("podman", harness=harness, extra_env={"FOO": "bar"})
    assert backend._extra_env == {"FOO": "bar"}


def test_create_openshell_with_extra_env(harness):
    backend = create_backend("openshell", harness=harness, extra_env={"FOO": "bar"})
    assert backend._extra_env == {"FOO": "bar"}


def test_create_openshell_with_approval_mode(harness):
    backend = create_backend("openshell", harness=harness, approval_mode="auto")
    assert backend.approval_mode == "auto"


def test_unknown_backend_raises(harness):
    with pytest.raises(ValueError, match="Unknown backend"):
        create_backend("docker", harness=harness)


def test_local_codex_credentials_fail_fast(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))

    backend = LocalBackend(workdir=str(tmp_path), harness=CodexHarness())

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        backend.setup()


def test_local_codex_accepts_auth_file(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    backend = LocalBackend(workdir=str(tmp_path), harness=CodexHarness())
    backend.setup()


def test_local_codex_passes_extra_openai_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    backend = LocalBackend(
        workdir=str(tmp_path),
        harness=CodexHarness(),
        extra_env={"OPENAI_API_KEY": "extra-key"},
    )
    process = mock.Mock(pid=123)

    with (
        mock.patch("agentic_ci.backends.local.subprocess.Popen", return_value=process) as popen,
        mock.patch.object(backend, "_process_stream", return_value=(0, False)),
    ):
        backend.run("prompt", "model", streaming=False)

    assert popen.call_args.kwargs["env"]["OPENAI_API_KEY"] == "extra-key"
    assert not any("extra-key" in arg for arg in popen.call_args.args[0])


def test_local_codex_extra_openai_key_overrides_global_key(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "global-key")

    backend = LocalBackend(
        workdir=str(tmp_path),
        harness=CodexHarness(),
        extra_env={"OPENAI_API_KEY": "extra-key"},
    )
    process = mock.Mock(pid=123)

    with (
        mock.patch("agentic_ci.backends.local.subprocess.Popen", return_value=process) as popen,
        mock.patch.object(backend, "_process_stream", return_value=(0, False)),
    ):
        backend.run("prompt", "model", streaming=False)

    child_env = popen.call_args.kwargs["env"]
    assert child_env["OPENAI_API_KEY"] == "extra-key"


def test_podman_codex_openai_key_is_passed_without_secret_in_args(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    backend = PodmanBackend(
        workdir=str(tmp_path),
        harness=CodexHarness(),
        extra_env={"OPENAI_API_KEY": "test-key"},
    )
    args = backend._build_env_args({"OPENAI_API_KEY": "test-key"})

    assert "OPENAI_API_KEY" in args
    assert not any("test-key" in arg for arg in args)


def test_backends_have_stop_method(harness):
    podman = create_backend("podman", harness=harness, workdir="/tmp")
    openshell = create_backend("openshell", harness=harness, workdir="/tmp")
    local = create_backend("local", harness=harness, workdir="/tmp")
    assert callable(getattr(podman, "stop", None))
    assert callable(getattr(openshell, "stop", None))
    assert callable(getattr(local, "stop", None))


def test_openshell_codex_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    backend = OpenShellBackend(workdir=str(tmp_path), harness=CodexHarness())

    with (
        mock.patch("agentic_ci.backends.openshell.gateway.is_running") as is_running,
        pytest.raises(RuntimeError, match="OPENAI_API_KEY"),
    ):
        backend.setup()

    is_running.assert_not_called()


def test_openshell_reuses_sandbox_when_auth_mode_matches(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state_path = tmp_path / "openshell-state.json"
    state_path.write_text(json.dumps({"auth_mode": "openai", "harness": "Codex", "image": None}))
    monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state_path))

    backend = OpenShellBackend(workdir=str(tmp_path), harness=CodexHarness())

    with (
        mock.patch("agentic_ci.backends.openshell.gateway.is_running", return_value=True),
        mock.patch("agentic_ci.backends.openshell.sandbox.exists", return_value=True),
        mock.patch("agentic_ci.backends.openshell.provider.provider_exists", return_value=True),
        mock.patch("agentic_ci.backends.openshell.provider.auth_mode", return_value="openai"),
        mock.patch("agentic_ci.backends.openshell.provider.setup") as setup_provider,
        mock.patch("agentic_ci.backends.openshell.sandbox.create") as create_sandbox,
    ):
        backend.setup()

    setup_provider.assert_not_called()
    create_sandbox.assert_not_called()


def test_openshell_recreates_sandbox_when_auth_mode_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state_path = tmp_path / "openshell-state.json"
    state_path.write_text(
        json.dumps({"auth_mode": "vertex", "harness": "Claude Code", "image": None})
    )
    monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state_path))

    backend = OpenShellBackend(workdir=str(tmp_path), harness=CodexHarness())

    with (
        mock.patch("agentic_ci.backends.openshell.gateway.is_running", return_value=True),
        mock.patch("agentic_ci.backends.openshell.sandbox.exists", return_value=True),
        mock.patch("agentic_ci.backends.openshell.provider.provider_exists", return_value=True),
        mock.patch("agentic_ci.backends.openshell.provider.auth_mode", return_value="vertex"),
        mock.patch("agentic_ci.backends.openshell.provider.delete") as delete_provider,
        mock.patch("agentic_ci.backends.openshell.sandbox.delete") as delete_sandbox,
        mock.patch("agentic_ci.backends.openshell.provider.setup") as setup_provider,
        mock.patch("agentic_ci.backends.openshell.sandbox.create") as create_sandbox,
        mock.patch.object(backend, "_run_setup_steps"),
        mock.patch.object(backend, "_upload_sandbox_config"),
        mock.patch("agentic_ci.backends.openshell.sandbox.upload"),
    ):
        backend.setup()

    delete_sandbox.assert_called_once_with()
    delete_provider.assert_called_once_with()
    setup_provider.assert_called_once_with(auth_mode="openai", env=mock.ANY)
    create_sandbox.assert_called_once()
    assert create_sandbox.call_args.kwargs["auth_mode"] == "openai"


def test_openshell_recreates_sandbox_when_identity_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    state_path = tmp_path / "openshell-state.json"
    state_path.write_text(
        json.dumps({"auth_mode": "openai", "harness": "Codex", "image": "old-image"})
    )
    monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state_path))

    backend = OpenShellBackend(workdir=str(tmp_path), harness=CodexHarness(), image="new-image")

    with (
        mock.patch("agentic_ci.backends.openshell.gateway.is_running", return_value=True),
        mock.patch("agentic_ci.backends.openshell.sandbox.exists", return_value=True),
        mock.patch("agentic_ci.backends.openshell.provider.provider_exists", return_value=True),
        mock.patch("agentic_ci.backends.openshell.provider.auth_mode", return_value="openai"),
        mock.patch("agentic_ci.backends.openshell.provider.setup") as setup_provider,
        mock.patch("agentic_ci.backends.openshell.sandbox.create") as create_sandbox,
        mock.patch("agentic_ci.backends.openshell.sandbox.delete") as delete_sandbox,
        mock.patch.object(backend, "_run_setup_steps"),
        mock.patch.object(backend, "_upload_sandbox_config"),
        mock.patch("agentic_ci.backends.openshell.sandbox.upload"),
    ):
        backend.setup()

    delete_sandbox.assert_called_once_with()
    setup_provider.assert_called_once_with(auth_mode="openai", env=mock.ANY)
    create_sandbox.assert_called_once()
    assert create_sandbox.call_args.kwargs["image"] == "new-image"


def test_openshell_passes_extra_env_to_provider(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state_path = tmp_path / "openshell-state.json"
    monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state_path))

    backend = OpenShellBackend(
        workdir=str(tmp_path),
        harness=CodexHarness(),
        extra_env={"OPENAI_API_KEY": "extra-key"},
    )

    with (
        mock.patch("agentic_ci.backends.openshell.gateway.is_running", return_value=True),
        mock.patch("agentic_ci.backends.openshell.sandbox.exists", return_value=False),
        mock.patch("agentic_ci.backends.openshell.provider.provider_exists", return_value=False),
        mock.patch("agentic_ci.backends.openshell.provider.validate_credentials") as validate,
        mock.patch("agentic_ci.backends.openshell.provider.setup") as setup_provider,
        mock.patch("agentic_ci.backends.openshell.sandbox.create"),
        mock.patch.object(backend, "_run_setup_steps"),
        mock.patch.object(backend, "_upload_sandbox_config"),
        mock.patch("agentic_ci.backends.openshell.sandbox.upload"),
    ):
        backend.setup()

    validate.assert_called_once_with("openai", mock.ANY)
    assert validate.call_args.args[1]["OPENAI_API_KEY"] == "extra-key"
    setup_provider.assert_called_once_with(auth_mode="openai", env=mock.ANY)
    assert setup_provider.call_args.kwargs["env"]["OPENAI_API_KEY"] == "extra-key"


def test_openshell_uses_extra_env_for_claude_auth_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    state_path = tmp_path / "openshell-state.json"
    monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state_path))

    backend = OpenShellBackend(
        workdir=str(tmp_path),
        harness=ClaudeCodeHarness(),
        extra_env={"ANTHROPIC_API_KEY": "extra-key"},
    )

    with (
        mock.patch("agentic_ci.backends.openshell.gateway.is_running", return_value=True),
        mock.patch("agentic_ci.backends.openshell.sandbox.exists", return_value=False),
        mock.patch("agentic_ci.backends.openshell.provider.provider_exists", return_value=False),
        mock.patch("agentic_ci.backends.openshell.provider.validate_credentials") as validate,
        mock.patch("agentic_ci.backends.openshell.provider.setup") as setup_provider,
        mock.patch("agentic_ci.backends.openshell.sandbox.create") as create_sandbox,
        mock.patch.object(backend, "_run_setup_steps"),
        mock.patch.object(backend, "_upload_sandbox_config"),
        mock.patch("agentic_ci.backends.openshell.sandbox.upload"),
    ):
        backend.setup()

    validate.assert_called_once_with("api-key", mock.ANY)
    setup_provider.assert_called_once_with(auth_mode="api-key", env=mock.ANY)
    assert create_sandbox.call_args.kwargs["auth_mode"] == "api-key"


class TestOpenShellEnvScript:
    """Tests for OpenShellBackend._write_env_script()."""

    def _capture_script(self, monkeypatch, tmp_path, **env_overrides):
        """Run _write_env_script with mocked sandbox ops and return the script content."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("AGENT_ENABLED_PLUGINS", raising=False)
        for key, val in env_overrides.items():
            if val is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, val)

        harness = ClaudeCodeHarness()
        backend = OpenShellBackend(workdir=str(tmp_path), harness=harness)

        captured = []

        def mock_upload(path):
            with open(path) as f:
                captured.append(f.read())

        with (
            mock.patch("agentic_ci.backends.openshell.sandbox.upload", side_effect=mock_upload),
            mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd"),
        ):
            backend._write_env_script("claude-opus-4-6")

        assert len(captured) == 1
        return captured[0]

    def test_env_script_calls_enable_plugins(self, monkeypatch, tmp_path):
        script = self._capture_script(monkeypatch, tmp_path)
        assert "command -v agentic-ci" in script
        assert "agentic-ci enable-plugins" in script

    def test_env_script_sets_seed_dir(self, monkeypatch, tmp_path):
        script = self._capture_script(monkeypatch, tmp_path)
        assert "CLAUDE_CODE_PLUGIN_SEED_DIR=/sandbox/.claude-seed" in script

    def test_env_script_includes_enabled_plugins_var(self, monkeypatch, tmp_path):
        script = self._capture_script(monkeypatch, tmp_path, AGENT_ENABLED_PLUGINS="alpha,beta")
        assert "AGENT_ENABLED_PLUGINS" in script
        assert "alpha,beta" in script

    def test_env_script_omits_enabled_plugins_when_unset(self, monkeypatch, tmp_path):
        script = self._capture_script(monkeypatch, tmp_path)
        assert "AGENT_ENABLED_PLUGINS" not in script

    def _capture_script_vertex(self, monkeypatch, tmp_path, **env_overrides):
        """Like _capture_script but with Vertex auth (no API key)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
        monkeypatch.delenv("AGENT_ENABLED_PLUGINS", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_MAX_RETRIES", raising=False)
        for key, val in env_overrides.items():
            if val is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, val)

        harness = ClaudeCodeHarness()
        backend = OpenShellBackend(workdir=str(tmp_path), harness=harness)

        captured = []

        def mock_upload(path):
            with open(path) as f:
                captured.append(f.read())

        with (
            mock.patch("agentic_ci.backends.openshell.sandbox.upload", side_effect=mock_upload),
            mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd"),
        ):
            backend._write_env_script("claude-opus-4-6")

        assert len(captured) == 1
        return captured[0]

    def test_env_script_sets_max_retries_for_vertex(self, monkeypatch, tmp_path):
        script = self._capture_script_vertex(monkeypatch, tmp_path)
        assert "CLAUDE_CODE_MAX_RETRIES=20" in script

    def test_env_script_max_retries_override(self, monkeypatch, tmp_path):
        script = self._capture_script_vertex(monkeypatch, tmp_path, CLAUDE_CODE_MAX_RETRIES="30")
        assert "CLAUDE_CODE_MAX_RETRIES=30" in script

    def test_env_script_omits_max_retries_for_api_key(self, monkeypatch, tmp_path):
        script = self._capture_script(monkeypatch, tmp_path)
        assert "CLAUDE_CODE_MAX_RETRIES" not in script

    def test_env_script_uses_extra_env_for_claude_api_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        backend = OpenShellBackend(
            workdir=str(tmp_path),
            harness=ClaudeCodeHarness(),
            extra_env={"ANTHROPIC_API_KEY": "TEST_MARKER"},
        )
        captured = []

        def mock_upload(path):
            with open(path) as f:
                captured.append(f.read())

        with (
            mock.patch("agentic_ci.backends.openshell.sandbox.upload", side_effect=mock_upload),
            mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd"),
        ):
            backend._write_env_script("claude-opus-4-6")

        assert "export ANTHROPIC_API_KEY=TEST_MARKER" in captured[0]
        assert "CLAUDE_CODE_USE_VERTEX" not in captured[0]

    def test_codex_env_script_contains_openai_key_for_l4_auth(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "super-secret")

        backend = OpenShellBackend(workdir=str(tmp_path), harness=CodexHarness())
        captured = []

        def mock_upload(path):
            with open(path) as env_script:
                captured.append(env_script.read())

        with (
            mock.patch("agentic_ci.backends.openshell.sandbox.upload", side_effect=mock_upload),
            mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd"),
        ):
            backend._write_env_script("gpt-5.6-sol")

        assert len(captured) == 1
        assert "export OPENAI_API_KEY=super-secret" in captured[0]


class TestTokenKeepalive:
    """Tests for _token_keepalive and its integration in OpenShellBackend.run()."""

    def test_keepalive_calls_rotate_token(self, monkeypatch):
        """After the phase offset, rotate_token is called on each interval tick."""
        monkeypatch.setattr("agentic_ci.backends.openshell._TOKEN_KEEPALIVE_OFFSET", 0)
        monkeypatch.setattr("agentic_ci.backends.openshell._TOKEN_KEEPALIVE_INTERVAL", 0)
        call_count = 0
        stop = threading.Event()

        def mock_rotate():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                stop.set()

        monkeypatch.setattr("agentic_ci.backends.openshell.provider.rotate_token", mock_rotate)
        _token_keepalive(stop)
        assert call_count >= 3

    def test_keepalive_stops_on_event_during_offset(self, monkeypatch):
        """Setting stop during the initial offset exits without rotating."""
        monkeypatch.setattr("agentic_ci.backends.openshell._TOKEN_KEEPALIVE_OFFSET", 10)
        rotate_called = False

        def mock_rotate():
            nonlocal rotate_called
            rotate_called = True

        monkeypatch.setattr("agentic_ci.backends.openshell.provider.rotate_token", mock_rotate)
        stop = threading.Event()
        stop.set()
        _token_keepalive(stop)
        assert not rotate_called

    def test_keepalive_logs_rotate_failure(self, monkeypatch, capsys):
        """CalledProcessError from rotate_token is logged, not raised."""
        import subprocess

        monkeypatch.setattr("agentic_ci.backends.openshell._TOKEN_KEEPALIVE_OFFSET", 0)
        monkeypatch.setattr("agentic_ci.backends.openshell._TOKEN_KEEPALIVE_INTERVAL", 0)
        stop = threading.Event()
        call_count = 0

        def mock_rotate():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise subprocess.CalledProcessError(1, "openshell", stderr="auth error")
            stop.set()

        monkeypatch.setattr("agentic_ci.backends.openshell.provider.rotate_token", mock_rotate)
        _token_keepalive(stop)
        captured = capsys.readouterr()
        assert "[token-keepalive] rotate failed" in captured.out
        assert "auth error" in captured.out
        assert call_count >= 2
