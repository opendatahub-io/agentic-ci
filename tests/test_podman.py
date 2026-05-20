"""Tests for Podman backend."""

import base64
import json
import os

import pytest

from agentic_ci.backends.podman import PodmanBackend


def test_find_credentials_from_gcloud_credentials_json(monkeypatch, tmp_path):
    creds = json.dumps({"type": "authorized_user", "client_id": "test"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)

    backend = PodmanBackend(workdir=str(tmp_path))
    result, source = backend._find_credentials()
    assert json.loads(result)["client_id"] == "test"
    assert source == "GCLOUD_CREDENTIALS env var"


def test_find_credentials_from_base64(monkeypatch, tmp_path):
    creds = json.dumps({"type": "service_account", "project_id": "test"})
    encoded = base64.b64encode(creds.encode()).decode()
    monkeypatch.setenv("GCLOUD_CREDENTIALS", encoded)

    backend = PodmanBackend(workdir=str(tmp_path))
    result, source = backend._find_credentials()
    assert json.loads(result)["project_id"] == "test"
    assert source == "GCLOUD_CREDENTIALS env var (base64)"


def test_find_credentials_from_service_account_key(monkeypatch, tmp_path):
    creds = json.dumps({"type": "service_account", "project_id": "sa-test"})
    encoded = base64.b64encode(creds.encode()).decode()
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY", encoded)

    backend = PodmanBackend(workdir=str(tmp_path))
    result, source = backend._find_credentials()
    assert json.loads(result)["project_id"] == "sa-test"
    assert source == "GCP_SERVICE_ACCOUNT_KEY env var"


def test_find_credentials_from_adc_file(monkeypatch, tmp_path):
    creds = json.dumps({"type": "authorized_user", "client_id": "file-test"})
    adc_path = tmp_path / "adc.json"
    adc_path.write_text(creds)
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc_path))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent"))

    backend = PodmanBackend(workdir=str(tmp_path))
    result, source = backend._find_credentials()
    assert json.loads(result)["client_id"] == "file-test"
    assert source == "GOOGLE_APPLICATION_CREDENTIALS file"


def test_find_credentials_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent"))

    backend = PodmanBackend(workdir=str(tmp_path))
    with pytest.raises(RuntimeError, match="No GCP credentials found"):
        backend._find_credentials()


def test_find_credentials_invalid_json_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("GCLOUD_CREDENTIALS", "not-json-not-base64")

    backend = PodmanBackend(workdir=str(tmp_path))
    with pytest.raises(RuntimeError, match="not valid JSON"):
        backend._find_credentials()


def test_build_env_args_basic(tmp_path):
    backend = PodmanBackend(workdir=str(tmp_path))
    args = backend._build_env_args()
    assert "--env" in args
    assert "CLAUDE_CODE_USE_VERTEX=1" in args
    assert "DISABLE_AUTOUPDATER=1" in args


def test_build_otel_exec_env(tmp_path):
    backend = PodmanBackend(workdir=str(tmp_path))
    args = backend._build_otel_exec_env(otel_port=4318)
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in args
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318" in args


def test_build_otel_exec_env_empty_without_port(tmp_path):
    backend = PodmanBackend(workdir=str(tmp_path))
    assert backend._build_otel_exec_env(otel_port=None) == []


def test_is_valid_json():
    assert PodmanBackend._is_valid_json('{"key": "value"}')
    assert not PodmanBackend._is_valid_json("not json")
    assert not PodmanBackend._is_valid_json("")


def test_try_base64_decode():
    encoded = base64.b64encode(b"hello").decode()
    assert PodmanBackend._try_base64_decode(encoded) == "hello"
    assert PodmanBackend._try_base64_decode("!!!invalid!!!") is None


def test_resolve_credentials_creates_config(monkeypatch, tmp_path):
    creds = json.dumps({"type": "authorized_user", "client_id": "test"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-project")

    backend = PodmanBackend(workdir=str(tmp_path))
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
