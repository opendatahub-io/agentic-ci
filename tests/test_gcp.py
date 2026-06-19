"""Tests for GCP credential resolution."""

import base64
import json

import pytest

from agentic_ci.gcp import (
    _is_valid_json,
    _try_base64_decode,
    ensure_adc,
    find_credentials,
    read_credential_type,
)


def test_find_credentials_from_gcloud_credentials_json(monkeypatch):
    creds = json.dumps({"type": "authorized_user", "client_id": "test"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)

    result, source = find_credentials()
    assert json.loads(result)["client_id"] == "test"
    assert source == "GCLOUD_CREDENTIALS env var"


def test_find_credentials_from_base64(monkeypatch):
    creds = json.dumps({"type": "service_account", "project_id": "test"})
    encoded = base64.b64encode(creds.encode()).decode()
    monkeypatch.setenv("GCLOUD_CREDENTIALS", encoded)

    result, source = find_credentials()
    assert json.loads(result)["project_id"] == "test"
    assert source == "GCLOUD_CREDENTIALS env var (base64)"


def test_find_credentials_from_service_account_key(monkeypatch):
    creds = json.dumps({"type": "service_account", "project_id": "sa-test"})
    encoded = base64.b64encode(creds.encode()).decode()
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY", encoded)

    result, source = find_credentials()
    assert json.loads(result)["project_id"] == "sa-test"
    assert source == "GCP_SERVICE_ACCOUNT_KEY env var"


def test_find_credentials_from_service_account_key_json_file(monkeypatch, tmp_path):
    creds = json.dumps({"type": "service_account", "project_id": "sa-file-test"})
    key_file = tmp_path / "sa-key.json"
    key_file.write_text(creds)
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY", str(key_file))

    result, source = find_credentials()
    assert json.loads(result)["project_id"] == "sa-file-test"
    assert source == "GCP_SERVICE_ACCOUNT_KEY file"


def test_find_credentials_from_service_account_key_base64_file(monkeypatch, tmp_path):
    creds = json.dumps({"type": "service_account", "project_id": "sa-b64-file"})
    encoded = base64.b64encode(creds.encode()).decode()
    key_file = tmp_path / "sa-key.b64"
    key_file.write_text(encoded)
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY", str(key_file))

    result, source = find_credentials()
    assert json.loads(result)["project_id"] == "sa-b64-file"
    assert source == "GCP_SERVICE_ACCOUNT_KEY file"


def test_find_credentials_from_service_account_key_file_invalid(monkeypatch, tmp_path):
    key_file = tmp_path / "bad-key.json"
    key_file.write_text("not valid json or base64")
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY", str(key_file))

    with pytest.raises(RuntimeError, match="not valid JSON or base64"):
        find_credentials()


def test_find_credentials_from_adc_file(monkeypatch, tmp_path):
    creds = json.dumps({"type": "authorized_user", "client_id": "file-test"})
    adc_file = tmp_path / "adc.json"
    adc_file.write_text(creds)
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc_file))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent"))

    result, source = find_credentials()
    assert json.loads(result)["client_id"] == "file-test"
    assert source == "GOOGLE_APPLICATION_CREDENTIALS file"


def test_find_credentials_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent"))

    with pytest.raises(RuntimeError, match="No GCP credentials found"):
        find_credentials()


def test_find_credentials_invalid_json_raises(monkeypatch):
    monkeypatch.setenv("GCLOUD_CREDENTIALS", "not-json-not-base64")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        find_credentials()


def test_is_valid_json():
    assert _is_valid_json('{"key": "value"}')
    assert not _is_valid_json("not json")
    assert not _is_valid_json("")


def test_try_base64_decode():
    encoded = base64.b64encode(b"hello").decode()
    assert _try_base64_decode(encoded) == "hello"
    assert _try_base64_decode("!!!invalid!!!") is None


def test_ensure_adc_writes_from_env_var(monkeypatch, tmp_path):
    creds = json.dumps({"type": "service_account", "project_id": "test"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    monkeypatch.setenv("HOME", str(tmp_path))

    source = ensure_adc()
    assert source == "GCLOUD_CREDENTIALS env var"

    adc_file = tmp_path / ".config" / "gcloud" / "application_default_credentials.json"
    assert adc_file.exists()
    assert json.loads(adc_file.read_text())["project_id"] == "test"


def test_ensure_adc_env_var_overrides_existing_file(monkeypatch, tmp_path):
    old_creds = json.dumps({"type": "authorized_user", "client_id": "stale"})
    adc_dir = tmp_path / ".config" / "gcloud"
    adc_dir.mkdir(parents=True)
    (adc_dir / "application_default_credentials.json").write_text(old_creds)

    new_creds = json.dumps({"type": "service_account", "project_id": "fresh"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", new_creds)
    monkeypatch.setenv("HOME", str(tmp_path))

    source = ensure_adc()
    assert source == "GCLOUD_CREDENTIALS env var"

    written = json.loads((adc_dir / "application_default_credentials.json").read_text())
    assert written["project_id"] == "fresh"


def test_ensure_adc_keeps_existing_when_no_env_vars(monkeypatch, tmp_path):
    creds = json.dumps({"type": "authorized_user", "client_id": "existing"})
    adc_dir = tmp_path / ".config" / "gcloud"
    adc_dir.mkdir(parents=True)
    (adc_dir / "application_default_credentials.json").write_text(creds)

    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    source = ensure_adc()
    assert "existing ADC file" in source

    written = json.loads((adc_dir / "application_default_credentials.json").read_text())
    assert written["client_id"] == "existing"


def test_ensure_adc_raises_when_no_creds(monkeypatch, tmp_path):
    monkeypatch.delenv("GCLOUD_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent"))

    with pytest.raises(RuntimeError, match="No GCP credentials found"):
        ensure_adc()


def test_read_credential_type_service_account(tmp_path, monkeypatch):
    creds = json.dumps({"type": "service_account"})
    adc_dir = tmp_path / ".config" / "gcloud"
    adc_dir.mkdir(parents=True)
    (adc_dir / "application_default_credentials.json").write_text(creds)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert read_credential_type() == "service_account"


def test_read_credential_type_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent"))
    assert read_credential_type() == "unknown"
