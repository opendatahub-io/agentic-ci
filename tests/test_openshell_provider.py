"""Tests for OpenShell provider token rotation."""

import subprocess
from unittest import mock

import pytest

from agentic_ci.backends.openshell.provider import (
    PROVIDER_NAME,
    rotate_token,
    setup,
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
    def test_openai_provider_uses_codex_api_key(self, monkeypatch):
        monkeypatch.setenv("CODEX_API_KEY", "test-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

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
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=False,
            ),
            pytest.raises(RuntimeError, match="CODEX_API_KEY"),
        ):
            setup("openai")
