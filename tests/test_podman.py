"""Tests for Podman backend."""

import base64
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


def test_find_credentials_from_gcloud_credentials_json(monkeypatch, tmp_path, claude_harness):
    creds = json.dumps({"type": "authorized_user", "client_id": "test"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)

    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    result, source = backend._find_credentials()
    assert json.loads(result)["client_id"] == "test"
    assert source == "GCLOUD_CREDENTIALS env var"


def test_find_credentials_from_base64(monkeypatch, tmp_path, claude_harness):
    creds = json.dumps({"type": "service_account", "project_id": "test"})
    encoded = base64.b64encode(creds.encode()).decode()
    monkeypatch.setenv("GCLOUD_CREDENTIALS", encoded)

    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    result, source = backend._find_credentials()
    assert json.loads(result)["project_id"] == "test"
    assert source == "GCLOUD_CREDENTIALS env var (base64)"


def test_find_credentials_from_service_account_key(monkeypatch, tmp_path, claude_harness):
    creds = json.dumps({"type": "service_account", "project_id": "sa-test"})
    encoded = base64.b64encode(creds.encode()).decode()
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY", encoded)

    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    result, source = backend._find_credentials()
    assert json.loads(result)["project_id"] == "sa-test"
    assert source == "GCP_SERVICE_ACCOUNT_KEY env var"


def test_find_credentials_from_adc_file(monkeypatch, tmp_path, claude_harness):
    creds = json.dumps({"type": "authorized_user", "client_id": "file-test"})
    adc_path = tmp_path / "adc.json"
    adc_path.write_text(creds)
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc_path))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent"))

    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    result, source = backend._find_credentials()
    assert json.loads(result)["client_id"] == "file-test"
    assert source == "GOOGLE_APPLICATION_CREDENTIALS file"


def test_find_credentials_raises_when_missing(monkeypatch, tmp_path, claude_harness):
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent"))

    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    with pytest.raises(RuntimeError, match="No GCP credentials found"):
        backend._find_credentials()


def test_find_credentials_invalid_json_raises(monkeypatch, tmp_path, claude_harness):
    monkeypatch.setenv("GCLOUD_CREDENTIALS", "not-json-not-base64")

    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    with pytest.raises(RuntimeError, match="not valid JSON"):
        backend._find_credentials()


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


def test_is_valid_json():
    assert PodmanBackend._is_valid_json('{"key": "value"}')
    assert not PodmanBackend._is_valid_json("not json")
    assert not PodmanBackend._is_valid_json("")


def test_try_base64_decode():
    encoded = base64.b64encode(b"hello").decode()
    assert PodmanBackend._try_base64_decode(encoded) == "hello"
    assert PodmanBackend._try_base64_decode("!!!invalid!!!") is None


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
    assert "/home/claude/.config/gcloud/" in mount_str


def test_build_vol_args_opencode_mount_target(monkeypatch, tmp_path, opencode_harness):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds = json.dumps({"type": "authorized_user"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    backend._resolve_credentials()
    vol_args = backend._build_vol_args()
    mount_str = " ".join(vol_args)
    assert "/home/agent/.config/gcloud/" in mount_str


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
