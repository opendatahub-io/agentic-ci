"""Tests for Podman backend."""

import json
import os
import subprocess as _subprocess

import pytest

from agentic_ci.backends.podman import PodmanBackend
from agentic_ci.harness import ClaudeCodeHarness, CodexHarness, OpenCodeHarness


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


def test_setup_codex_credentials_fail_fast(monkeypatch, tmp_path):
    for name in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/codex:test",
        harness=CodexHarness(),
    )

    with pytest.raises(RuntimeError, match="CODEX_API_KEY"):
        backend.setup()


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


def test_container_name_is_unique(tmp_path, claude_harness):
    """Each PodmanBackend instance must get a unique container name."""
    b1 = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    b2 = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    assert b1._container_name != b2._container_name


def test_container_name_has_prefix(tmp_path, claude_harness):
    """Container name must start with 'agentic-ci-' for easy identification."""
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    assert backend._container_name.startswith("agentic-ci-")
    # The suffix is a full 32-char hex string (uuid4)
    suffix = backend._container_name[len("agentic-ci-") :]
    assert len(suffix) == 32
    int(suffix, 16)  # raises ValueError if not valid hex


def test_setup_uses_instance_container_name(monkeypatch, tmp_path, claude_harness):
    """setup() must use the instance's unique container name, not a global constant."""
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

    # rm call should use instance name
    rm_calls = [c for c in calls if c[:2] == ["podman", "rm"]]
    assert len(rm_calls) == 1
    assert rm_calls[0][3] == backend._container_name

    # run call should use instance name
    run_calls = [c for c in calls if c[:2] == ["podman", "run"]]
    assert len(run_calls) == 1
    name_idx = run_calls[0].index("--name")
    assert run_calls[0][name_idx + 1] == backend._container_name


def test_stop_uses_instance_container_name(monkeypatch, tmp_path, claude_harness):
    """stop() must use the instance's unique container name."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/test:latest",
        harness=claude_harness,
    )

    calls = []

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        return _subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    backend.stop()

    assert len(calls) == 1
    assert calls[0] == ["podman", "rm", "-f", backend._container_name]


def test_is_local_image_localhost(tmp_path, claude_harness):
    """localhost/ images are always considered local."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/myimage:latest",
        harness=claude_harness,
    )
    assert backend._is_local_image() is True


def test_is_local_image_cached_remote(monkeypatch, tmp_path, claude_harness):
    """Non-localhost images that exist locally should be treated as local."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="ghcr.io/org/image:latest",
        harness=claude_harness,
    )

    def mock_run(cmd, **kwargs):
        if cmd[:3] == ["podman", "image", "exists"]:
            return _subprocess.CompletedProcess(cmd, 0)
        return _subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    assert backend._is_local_image() is True


def test_is_local_image_missing_remote(monkeypatch, tmp_path, claude_harness):
    """Non-localhost images not present locally should not be treated as local."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="ghcr.io/org/image:latest",
        harness=claude_harness,
    )

    def mock_run(cmd, **kwargs):
        if cmd[:3] == ["podman", "image", "exists"]:
            return _subprocess.CompletedProcess(cmd, 1)
        return _subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    assert backend._is_local_image() is False
