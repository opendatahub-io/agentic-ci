"""Tests for Podman backend."""

import json
import os

import pytest

from agentic_ci.backends.podman import PodmanBackend
from agentic_ci.harness import ClaudeCodeHarness, OpenCodeHarness


@pytest.fixture()
def claude_harness():
    return ClaudeCodeHarness()


@pytest.fixture()
def opencode_harness():
    return OpenCodeHarness()


def test_build_env_args_claude_code(tmp_path, claude_harness):
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    args = backend._build_env_args()
    assert "--env" in args
    assert "CLAUDE_CODE_USE_VERTEX=1" in args
    assert "DISABLE_AUTOUPDATER=1" in args


def test_build_env_args_opencode(tmp_path, opencode_harness, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("VERTEX_LOCATION", "us-east1")
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    args = backend._build_env_args()
    assert "GOOGLE_CLOUD_PROJECT=my-proj" in args
    assert "VERTEX_LOCATION=us-east1" in args
    assert "OPENCODE_DISABLE_AUTOUPDATE=1" in args
    assert "CLAUDE_CODE_USE_VERTEX=1" not in args


def test_build_env_args_extra_env(tmp_path, claude_harness):
    backend = PodmanBackend(
        workdir=str(tmp_path), harness=claude_harness, extra_env={"MY_VAR": "value"}
    )
    args = backend._build_env_args()
    assert "MY_VAR=value" in args


def test_resolve_credentials_creates_config(monkeypatch, tmp_path, claude_harness):
    creds = json.dumps({"type": "authorized_user", "client_id": "test"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-project")

    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    backend._resolve_credentials()

    assert backend._config_dir is not None
    adc_path = os.path.join(
        backend._config_dir, ".config", "gcloud", "application_default_credentials.json"
    )
    assert os.path.isfile(adc_path)
    with open(adc_path) as f:
        assert json.loads(f.read())["client_id"] == "test"

    config_path = os.path.join(
        backend._config_dir, ".config", "gcloud", "configurations", "config_default"
    )
    assert os.path.isfile(config_path)
    with open(config_path) as f:
        content = f.read()
    assert "my-project" in content


def test_resolve_image_claude_code(monkeypatch, tmp_path, claude_harness):
    monkeypatch.setenv("CLAUDE_CONTAINER_IMAGE", "my-claude-image:latest")
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    backend._resolve_image()
    assert backend.image == "my-claude-image:latest"


def test_resolve_image_opencode(monkeypatch, tmp_path, opencode_harness):
    monkeypatch.setenv("OPENCODE_CONTAINER_IMAGE", "my-opencode-image:latest")
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    backend._resolve_image()
    assert backend.image == "my-opencode-image:latest"


def test_resolve_image_raises_with_correct_env_var(monkeypatch, tmp_path, opencode_harness):
    monkeypatch.delenv("OPENCODE_CONTAINER_IMAGE", raising=False)
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    with pytest.raises(RuntimeError, match="OPENCODE_CONTAINER_IMAGE"):
        backend._resolve_image()


def test_build_vol_args_claude_mount_target(monkeypatch, tmp_path, claude_harness):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds = json.dumps({"type": "authorized_user"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    backend._resolve_credentials()
    vol_args = backend._build_vol_args()
    mount_str = " ".join(vol_args)
    assert "/home/agent-ci/.config/gcloud/" in mount_str


def test_build_vol_args_opencode_mount_target(monkeypatch, tmp_path, opencode_harness):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds = json.dumps({"type": "authorized_user"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    backend._resolve_credentials()
    vol_args = backend._build_vol_args()
    mount_str = " ".join(vol_args)
    assert "/home/agent-ci/.config/gcloud/" in mount_str


def test_build_vol_args_api_key_no_gcloud_mounts(monkeypatch, tmp_path, claude_harness):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    vol_args = backend._build_vol_args()
    mount_str = " ".join(vol_args)
    assert "/workspace" in mount_str
    assert ".config/gcloud" not in mount_str


def test_build_env_args_api_key(monkeypatch, tmp_path, claude_harness):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    args = backend._build_env_args()
    assert "ANTHROPIC_API_KEY" in args
    assert "ANTHROPIC_API_KEY=sk-test-key" not in args
    assert "CLAUDE_CODE_USE_VERTEX=1" not in args


def test_setup_does_not_override_entrypoint(monkeypatch, tmp_path, claude_harness):
    """setup() passes sleep as the command, not --entrypoint, so the image entrypoint runs."""
    import subprocess as _subprocess

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/test:latest",
        harness=claude_harness,
    )

    calls = []
    original_run = _subprocess.run

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["podman", "rm"]:
            return _subprocess.CompletedProcess(cmd, 0)
        if cmd[:2] == ["podman", "run"]:
            return _subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["podman", "container", "inspect"]:
            return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    backend.setup()

    run_calls = [c for c in calls if c[:2] == ["podman", "run"]]
    assert len(run_calls) == 1
    run_cmd = run_calls[0]
    assert "--entrypoint" not in run_cmd
    image_idx = run_cmd.index("localhost/test:latest")
    assert run_cmd[image_idx + 1 : image_idx + 4] == ["bash", "-c", "sleep 1200"]
