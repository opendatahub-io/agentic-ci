"""Tests for OpenShell provider token rotation."""

import subprocess
from unittest import mock

import pytest

from agentic_ci.backends.openshell.provider import (
    PROVIDER_NAME,
    auth_mode,
    delete,
    rotate_token,
    setup,
    validate_credentials,
)


class TestRotateToken:
    def test_rotate_token_calls_openshell(self):
        with mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run:
            rotate_token()

        mock_run.assert_called_once_with(
            [
                "openshell",
                "provider",
                "refresh",
                "rotate",
                "--credential-key",
                "GCP_SA_ACCESS_TOKEN",
                PROVIDER_NAME,
            ],
            check=True,
        )

    def test_rotate_token_propagates_failure(self):
        with mock.patch(
            "agentic_ci.backends.openshell.provider._run",
            side_effect=subprocess.CalledProcessError(1, "openshell"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                rotate_token()


class TestProviderSetup:
    def test_validate_openai_provider_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            validate_credentials("openai")

    def test_openai_provider_accepts_explicit_environment(self):
        validate_credentials("openai", {"OPENAI_API_KEY": "extra-key"})

    def test_auth_mode_reads_provider_type(self):
        result = subprocess.CompletedProcess(
            ["openshell", "provider", "list"],
            0,
            stdout='{"providers": [{"name": "ci-gcp", "type": "openai"}]}',
        )
        with mock.patch(
            "agentic_ci.backends.openshell.provider._run", return_value=result
        ) as mock_run:
            assert auth_mode() == "openai"

        mock_run.assert_called_once_with(
            ["openshell", "provider", "list", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_delete_removes_provider(self):
        with mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run:
            delete()

        mock_run.assert_called_once_with(
            ["openshell", "provider", "delete", PROVIDER_NAME],
            check=True,
        )

    def test_openai_provider_uses_openai_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=False,
            ),
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
        ):
            setup("openai")

        args = mock_run.call_args.args[0]
        env = mock_run.call_args.kwargs["env"]
        assert args == [
            "openshell",
            "provider",
            "create",
            "--name",
            PROVIDER_NAME,
            "--type",
            "openai",
            "--credential",
            "OPENAI_API_KEY",
        ]
        assert env["OPENAI_API_KEY"] == "test-key"

    def test_openai_provider_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=False,
            ),
            pytest.raises(RuntimeError, match="OPENAI_API_KEY"),
        ):
            setup("openai")

    def test_openai_provider_uses_explicit_environment(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=False,
            ),
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
        ):
            setup("openai", {"OPENAI_API_KEY": "extra-key"})

        assert mock_run.call_args.kwargs["env"]["OPENAI_API_KEY"] == "extra-key"
