"""Tests for OpenShell provider setup and token rotation."""

import subprocess
from unittest import mock

import pytest

from agentic_ci.backends.openshell.provider import PROVIDER_NAME, rotate_token, setup


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
    def test_skips_creation_when_provider_matches_cursor_api_key(self):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider._get_provider_details",
                return_value={"type": "generic", "credential": "CURSOR_API_KEY"},
            ),
            mock.patch("agentic_ci.backends.openshell.provider._delete_provider") as mock_delete,
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_cursor_provider"
            ) as mock_cursor,
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_anthropic_provider"
            ) as mock_anthropic,
        ):
            setup(auth_mode="api-key", harness_name="cursor")
        mock_delete.assert_not_called()
        mock_cursor.assert_not_called()
        mock_anthropic.assert_not_called()

    def test_recreates_provider_when_harness_switches_anthropic_to_cursor(self):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider._get_provider_details",
                return_value={"type": "anthropic", "credential": "ANTHROPIC_API_KEY"},
            ),
            mock.patch("agentic_ci.backends.openshell.provider._delete_provider") as mock_delete,
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_cursor_provider"
            ) as mock_create,
        ):
            setup(auth_mode="api-key", harness_name="cursor")
        mock_delete.assert_called_once()
        mock_create.assert_called_once()

    def test_recreates_provider_when_harness_switches_cursor_to_anthropic(self):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider._get_provider_details",
                return_value={"type": "generic", "credential": "CURSOR_API_KEY"},
            ),
            mock.patch("agentic_ci.backends.openshell.provider._delete_provider") as mock_delete,
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_anthropic_provider"
            ) as mock_create,
        ):
            setup(auth_mode="api-key", harness_name="Claude Code")
        mock_delete.assert_called_once()
        mock_create.assert_called_once()

    def test_legacy_display_name_cursor_still_selects_cursor_provider(self):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider._get_provider_details", return_value=None
            ),
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_cursor_provider"
            ) as mock_create,
        ):
            setup(auth_mode="api-key", harness_name="Cursor")
        mock_create.assert_called_once()

    def test_recreates_provider_when_auth_mode_switches_api_key_to_vertex(self):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider._get_provider_details",
                return_value={"type": "anthropic", "credential": "ANTHROPIC_API_KEY"},
            ),
            mock.patch("agentic_ci.backends.openshell.provider._delete_provider") as mock_delete,
            mock.patch("agentic_ci.backends.openshell.provider._create_gcp_provider") as mock_gcp,
        ):
            setup(auth_mode="vertex", harness_name="Claude Code")
        mock_delete.assert_called_once()
        mock_gcp.assert_called_once()

    def test_recreates_provider_when_cursor_credential_mismatch_detected(self):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider._get_provider_details",
                return_value={"type": "generic", "credential": "ANTHROPIC_API_KEY"},
            ),
            mock.patch("agentic_ci.backends.openshell.provider._delete_provider") as mock_delete,
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_cursor_provider"
            ) as mock_create,
        ):
            setup(auth_mode="api-key", harness_name="cursor")
        mock_delete.assert_called_once()
        mock_create.assert_called_once()

    def test_cursor_api_key_creates_cursor_provider_when_missing(self):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider._get_provider_details", return_value=None
            ),
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_cursor_provider"
            ) as mock_create,
        ):
            setup(auth_mode="api-key", harness_name="cursor")
        mock_create.assert_called_once()

    def test_api_key_without_cursor_creates_anthropic_provider_when_missing(self):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider._get_provider_details", return_value=None
            ),
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_anthropic_provider"
            ) as mock_create,
        ):
            setup(auth_mode="api-key", harness_name=None)
        mock_create.assert_called_once()
