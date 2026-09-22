"""Tests for OpenShell provider token rotation."""

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from agentic_ci.backends.openshell import provider
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
                "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN",
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
    @pytest.fixture(autouse=True)
    def profiles_already_imported(self):
        """Profile import is covered by TestEnsureProfiles; skip it here."""
        with mock.patch("agentic_ci.backends.openshell.provider.ensure_profiles"):
            yield

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


class TestEnsureProfiles:
    def _list_result(self, stdout):
        return subprocess.CompletedProcess(
            ["openshell", "provider", "list-profiles"], 0, stdout=stdout
        )

    def test_imports_missing_profiles_only(self):
        listed = self._list_result('{"profiles": [{"id": "openai", "display_name": "OpenAI"}]}')
        with mock.patch(
            "agentic_ci.backends.openshell.provider._run", return_value=listed
        ) as mock_run:
            provider.ensure_profiles()

        imported = [
            call.args[0][5]
            for call in mock_run.call_args_list
            if call.args[0][:4] == ["openshell", "provider", "profile", "import"]
        ]
        assert [Path(p).name for p in imported] == ["google-vertex-ai.yaml", "anthropic.yaml"]
        for path in imported:
            assert Path(path).is_file()
        import_calls = [
            call
            for call in mock_run.call_args_list
            if call.args[0][:4] == ["openshell", "provider", "profile", "import"]
        ]
        assert all("--global" in call.args[0] for call in import_calls)
        assert all(call.kwargs.get("check") is True for call in import_calls)

    def test_imports_everything_when_listing_fails(self):
        failed = subprocess.CompletedProcess(["openshell"], 1, stdout="")
        with mock.patch(
            "agentic_ci.backends.openshell.provider._run", return_value=failed
        ) as mock_run:
            provider.ensure_profiles()

        imported = [
            Path(call.args[0][5]).stem
            for call in mock_run.call_args_list
            if call.args[0][:4] == ["openshell", "provider", "profile", "import"]
        ]
        assert imported == list(provider.PROFILE_IDS)

    def test_skips_import_when_all_present(self):
        listed = self._list_result(
            '[{"id": "google-vertex-ai"}, {"id": "openai"}, {"id": "anthropic"}]'
        )
        with mock.patch(
            "agentic_ci.backends.openshell.provider._run", return_value=listed
        ) as mock_run:
            provider.ensure_profiles()

        assert mock_run.call_count == 1

    def test_vendored_profiles_exist_for_every_id(self):
        for profile_id in provider.PROFILE_IDS:
            assert (provider.PROFILES_DIR / f"{profile_id}.yaml").is_file()

    def test_setup_imports_profiles_before_creating_provider(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        order = []
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists", return_value=False
            ),
            mock.patch(
                "agentic_ci.backends.openshell.provider.ensure_profiles",
                side_effect=lambda: order.append("profiles"),
            ),
            mock.patch(
                "agentic_ci.backends.openshell.provider._create_openai_provider",
                side_effect=lambda env: order.append("create"),
            ),
        ):
            provider.setup("openai")

        assert order == ["profiles", "create"]

    def test_setup_skips_profiles_when_provider_exists(self):
        with (
            mock.patch("agentic_ci.backends.openshell.provider.provider_exists", return_value=True),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_profiles") as ensure,
        ):
            provider.setup("openai")

        ensure.assert_not_called()


class TestVertexProvider:
    def test_adc_creates_google_vertex_ai_provider(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
        monkeypatch.setenv("CLOUD_ML_REGION", "global")
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists", return_value=False
            ),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_profiles"),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_adc", return_value="adc"),
            mock.patch(
                "agentic_ci.backends.openshell.provider._adc_credential_type",
                return_value="authorized_user",
            ),
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
        ):
            setup("vertex")

        args = mock_run.call_args.args[0]
        assert args[: args.index("--from-gcloud-adc")] == [
            "openshell",
            "provider",
            "create",
            "--name",
            PROVIDER_NAME,
            "--type",
            "google-vertex-ai",
        ]
        assert "--config" in args
        assert "VERTEX_AI_PROJECT_ID=proj" in args
        assert "VERTEX_AI_REGION=global" in args
        assert not any(a.startswith("project_id=") for a in args)

    def test_service_account_configures_vertex_token_refresh(self, monkeypatch, tmp_path):
        key = tmp_path / "sa.json"
        key.write_text(
            '{"type": "service_account", "client_email": "sa@proj.iam", "private_key": "PEM"}'
        )
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists", return_value=False
            ),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_profiles"),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_adc", return_value="env"),
            mock.patch(
                "agentic_ci.backends.openshell.provider._adc_credential_type",
                return_value="service_account",
            ),
            mock.patch("agentic_ci.backends.openshell.provider._adc_path", return_value=str(key)),
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
        ):
            setup("vertex")

        calls = [c.args[0] for c in mock_run.call_args_list]
        create = calls[0]
        assert create[create.index("--type") + 1] == "google-vertex-ai"
        assert "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN=placeholder" in create
        configure = calls[1]
        assert configure[:5] == [
            "openshell",
            "provider",
            "refresh",
            "configure",
            "--credential-key",
        ]
        assert configure[5] == "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN"
        assert "client_email=sa@proj.iam" in configure
        rotate = calls[2]
        assert rotate[3] == "rotate"
        assert (
            rotate[rotate.index("--credential-key") + 1] == "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN"
        )
