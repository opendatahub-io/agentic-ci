"""Tests for backend factory."""

from unittest import mock

import pytest

from agentic_ci.backends import create_backend
from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.backends.podman import PodmanBackend
from agentic_ci.harness import ClaudeCodeHarness, create_harness


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


def test_create_podman_with_image(harness):
    backend = create_backend("podman", harness=harness, image="my-image:latest")
    assert backend.image == "my-image:latest"


def test_create_openshell_with_policy(harness):
    backend = create_backend("openshell", harness=harness, policy="/path/to/policy.yml")
    assert backend.policy_path == "/path/to/policy.yml"


def test_create_podman_with_timeout(harness):
    backend = create_backend("podman", harness=harness, timeout=600)
    assert backend.timeout == 600


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


def test_backends_have_stop_method(harness):
    podman = create_backend("podman", harness=harness, workdir="/tmp")
    openshell = create_backend("openshell", harness=harness, workdir="/tmp")
    assert callable(getattr(podman, "stop", None))
    assert callable(getattr(openshell, "stop", None))


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
