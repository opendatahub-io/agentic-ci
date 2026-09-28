"""Tests for OpenShell provider token rotation."""

import subprocess
from importlib.resources import files
from unittest import mock

import pytest
import yaml

from agentic_ci.backends.openshell.provider import (
    ANTHROPIC_PROFILE_ID,
    OPENAI_PROFILE_ID,
    PROVIDER_NAME,
    api_key_env_var,
    auth_mode,
    credential_fingerprint,
    delete,
    ensure_profile,
    profile_endpoint_hosts,
    refresh_credentials,
    rotate_token,
    setup,
    validate_credentials,
)
from agentic_ci.backends.openshell.sandbox import AGENT_BINARY_PATHS


def _packaged_profile(profile_id):
    resource = files("agentic_ci.backends.openshell").joinpath("profiles", f"{profile_id}.yaml")
    return yaml.safe_load(resource.read_text(encoding="utf-8"))


def _exported(profile, resource_version=3):
    """Mimic ``openshell provider profile export``: server fields and defaults added."""
    exported = {**profile, "resource_version": resource_version, "source": "user"}
    exported["credentials"] = [{**c, "query_param": ""} for c in profile["credentials"]]
    return yaml.safe_dump(exported)


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

    @pytest.mark.parametrize(
        ("provider_type", "expected"),
        [
            (OPENAI_PROFILE_ID, "openai"),
            (ANTHROPIC_PROFILE_ID, "api-key"),
            ("google-cloud", "vertex"),
        ],
    )
    def test_auth_mode_reads_provider_type(self, provider_type, expected):
        result = subprocess.CompletedProcess(
            ["openshell", "provider", "list"],
            0,
            stdout=f'{{"providers": [{{"name": "ci-gcp", "type": "{provider_type}"}}]}}',
        )
        with mock.patch(
            "agentic_ci.backends.openshell.provider._run", return_value=result
        ) as mock_run:
            assert auth_mode() == expected

        mock_run.assert_called_once_with(
            ["openshell", "provider", "list", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )

    @pytest.mark.parametrize(
        ("provider_type", "mode"), [("openai", "openai"), ("anthropic", "api-key")]
    )
    def test_auth_mode_flags_builtin_profile_providers(self, provider_type, mode):
        # Providers from the builtin profiles bind the API host to curl, so
        # they must not look reusable for the auth mode they serve.
        result = subprocess.CompletedProcess(
            ["openshell", "provider", "list"],
            0,
            stdout=f'{{"providers": [{{"name": "ci-gcp", "type": "{provider_type}"}}]}}',
        )
        with mock.patch("agentic_ci.backends.openshell.provider._run", return_value=result):
            assert auth_mode() != mode

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
            OPENAI_PROFILE_ID,
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

    def test_setup_refreshes_key_of_existing_openai_provider(self):
        # Codex authenticates only through the provider placeholder, so a
        # reused provider must get the current key, not the one it was
        # created with.
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=True,
            ),
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
        ):
            setup("openai", {"OPENAI_API_KEY": "rotated-key"})

        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == [
            "openshell",
            "provider",
            "update",
            PROVIDER_NAME,
            "--credential",
            "OPENAI_API_KEY",
        ]
        assert mock_run.call_args.kwargs["check"] is True
        assert mock_run.call_args.kwargs["env"]["OPENAI_API_KEY"] == "rotated-key"

    @pytest.mark.parametrize("exists", [False, True])
    @pytest.mark.parametrize("raw_key", ["sk-fake-key\n", "sk-fake-key\r\n", "  sk-fake-key \t"])
    def test_openai_provider_stores_key_without_surrounding_whitespace(self, raw_key, exists):
        # OpenShell refuses to inject a secret containing CR or LF and
        # answers HTTP 500, which Codex reports as "high demand". A CI
        # secret pasted with a trailing newline must reach the provider
        # stripped, as `codex login --with-api-key` strips it.
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=exists,
            ),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_profile"),
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
        ):
            setup("openai", {"OPENAI_API_KEY": raw_key})

        assert mock_run.call_args.args[0][2] == ("update" if exists else "create")
        assert mock_run.call_args.kwargs["env"]["OPENAI_API_KEY"] == "sk-fake-key"
        assert "sk-fake-key" not in mock_run.call_args.args[0]

    @pytest.mark.parametrize("exists", [False, True])
    @pytest.mark.parametrize("raw_key", ["sk-fake\nkey", "sk-fake\rkey", "sk-fake\0key"])
    def test_openai_key_with_inner_line_break_is_rejected(self, raw_key, exists):
        env = {"OPENAI_API_KEY": raw_key}
        with pytest.raises(RuntimeError, match="line break or NUL"):
            validate_credentials("openai", env)
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=exists,
            ),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_profile"),
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
            pytest.raises(RuntimeError, match="line break or NUL"),
        ):
            setup("openai", env)

        mock_run.assert_not_called()

    @pytest.mark.parametrize("exists", [False, True])
    @pytest.mark.parametrize(
        ("raw_key", "kind"),
        [
            ("sk-fake-key\n", "(a line break)"),
            ("sk-fake-key\r\n", "(a line break)"),
            ("  sk-fake-key \t", "(spaces or tabs)"),
        ],
    )
    def test_stripping_the_openai_key_is_reported(self, raw_key, kind, exists, capsys):
        # The run log must show whether the secret had surrounding
        # whitespace, without printing the key.
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=exists,
            ),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_profile"),
            mock.patch("agentic_ci.backends.openshell.provider._run"),
        ):
            setup("openai", {"OPENAI_API_KEY": raw_key})

        out = capsys.readouterr().out
        assert out.count("Stripped surrounding whitespace") == 1
        assert f"{kind} from OPENAI_API_KEY" in out
        assert "sk-fake-key" not in out

    @pytest.mark.parametrize("exists", [False, True])
    def test_clean_openai_key_is_not_reported_as_stripped(self, exists, capsys):
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=exists,
            ),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_profile"),
            mock.patch("agentic_ci.backends.openshell.provider._run"),
        ):
            setup("openai", {"OPENAI_API_KEY": "sk-fake-key"})

        assert "Stripped" not in capsys.readouterr().out

    def test_blank_openai_key_is_missing(self):
        with pytest.raises(RuntimeError, match="require OPENAI_API_KEY"):
            validate_credentials("openai", {"OPENAI_API_KEY": " \n"})

    def test_credential_fingerprint_ignores_surrounding_whitespace(self):
        assert credential_fingerprint("openai", {"OPENAI_API_KEY": "key-a\n"}) == (
            credential_fingerprint("openai", {"OPENAI_API_KEY": "key-a"})
        )

    @pytest.mark.parametrize("mode", ["api-key", "vertex", "oauth"])
    def test_refresh_credentials_leaves_other_auth_modes_alone(self, mode):
        with mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run:
            refresh_credentials(mode, {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"})

        mock_run.assert_not_called()

    def test_refresh_credentials_requires_openai_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with (
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
            pytest.raises(RuntimeError, match="OPENAI_API_KEY"),
        ):
            refresh_credentials("openai")

        mock_run.assert_not_called()

    def test_credential_fingerprint_tracks_openai_key_only(self):
        first = credential_fingerprint("openai", {"OPENAI_API_KEY": "key-a"})

        assert first == credential_fingerprint("openai", {"OPENAI_API_KEY": "key-a"})
        assert first != credential_fingerprint("openai", {"OPENAI_API_KEY": "key-b"})
        assert "key-a" not in first
        assert len(first) == 16
        for mode in ("api-key", "vertex", "oauth"):
            assert credential_fingerprint(mode, {"ANTHROPIC_API_KEY": "k"}) is None

    def test_oauth_mode_creates_no_provider(self):
        # The OAuth token reaches the sandbox through the env script only;
        # OpenShell has no provider profile for it.
        with mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run:
            setup("oauth", {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"})

        mock_run.assert_not_called()


class TestProviderProfiles:
    @pytest.mark.parametrize(
        ("profile_id", "host", "agent_binaries"),
        [
            (OPENAI_PROFILE_ID, "api.openai.com", {"codex"}),
            (ANTHROPIC_PROFILE_ID, "api.anthropic.com", {"claude", "opencode"}),
        ],
    )
    def test_packaged_profile_binds_only_agent_binaries(self, profile_id, host, agent_binaries):
        profile = _packaged_profile(profile_id)

        assert profile["id"] == profile_id
        assert profile["binaries"]
        assert set(profile["binaries"]) <= set(AGENT_BINARY_PATHS)
        assert {path.rsplit("/", 1)[1] for path in profile["binaries"]} == agent_binaries
        (endpoint,) = profile["endpoints"]
        assert endpoint["host"] == host
        # An L7 rule next to agentic-ci's L4 agent rule makes OpenShell refuse
        # the agent's CONNECT, so the endpoint must stay L4 with the opt-in.
        assert "protocol" not in endpoint
        assert endpoint["allow_uninspected_credentials"] is True

    def test_ensure_profile_imports_missing_profile(self):
        missing = subprocess.CompletedProcess(["openshell"], 1, stdout="", stderr="not found")
        imported = []

        def fake_run(args, **kwargs):
            if args[3] == "import":
                with open(args[5]) as f:
                    imported.append(yaml.safe_load(f))
                return subprocess.CompletedProcess(args, 0)
            return missing

        with mock.patch(
            "agentic_ci.backends.openshell.provider._run", side_effect=fake_run
        ) as mock_run:
            ensure_profile(OPENAI_PROFILE_ID)

        assert mock_run.call_args_list[0].args[0] == [
            "openshell",
            "provider",
            "profile",
            "export",
            OPENAI_PROFILE_ID,
            "-o",
            "yaml",
        ]
        assert mock_run.call_args_list[1].args[0][:5] == [
            "openshell",
            "provider",
            "profile",
            "import",
            "-f",
        ]
        assert mock_run.call_args_list[1].kwargs == {"check": True}
        assert imported == [_packaged_profile(OPENAI_PROFILE_ID)]

    def test_ensure_profile_keeps_matching_profile(self):
        current = subprocess.CompletedProcess(
            ["openshell"], 0, stdout=_exported(_packaged_profile(OPENAI_PROFILE_ID))
        )
        with mock.patch(
            "agentic_ci.backends.openshell.provider._run", return_value=current
        ) as mock_run:
            ensure_profile(OPENAI_PROFILE_ID)

        mock_run.assert_called_once()

    def test_ensure_profile_updates_drifted_profile(self):
        stale = {**_packaged_profile(OPENAI_PROFILE_ID), "binaries": ["/usr/bin/curl"]}
        current = subprocess.CompletedProcess(["openshell"], 0, stdout=_exported(stale, 7))
        updated = []

        def fake_run(args, **kwargs):
            if args[3] == "update":
                with open(args[6]) as f:
                    updated.append(yaml.safe_load(f))
                return subprocess.CompletedProcess(args, 0)
            return current

        with mock.patch(
            "agentic_ci.backends.openshell.provider._run", side_effect=fake_run
        ) as mock_run:
            ensure_profile(OPENAI_PROFILE_ID)

        assert mock_run.call_args_list[1].args[0][:6] == [
            "openshell",
            "provider",
            "profile",
            "update",
            OPENAI_PROFILE_ID,
            "-f",
        ]
        assert updated == [{**_packaged_profile(OPENAI_PROFILE_ID), "resource_version": 7}]

    def test_provider_creation_registers_profile_first(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        calls = []
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists",
                return_value=False,
            ),
            mock.patch(
                "agentic_ci.backends.openshell.provider.ensure_profile",
                side_effect=lambda profile_id: calls.append(("profile", profile_id)),
            ),
            mock.patch(
                "agentic_ci.backends.openshell.provider._run",
                side_effect=lambda args, **kwargs: calls.append(("run", args)),
            ),
        ):
            setup("api-key")

        assert calls == [
            ("profile", ANTHROPIC_PROFILE_ID),
            (
                "run",
                [
                    "openshell",
                    "provider",
                    "create",
                    "--name",
                    PROVIDER_NAME,
                    "--type",
                    ANTHROPIC_PROFILE_ID,
                    "--credential",
                    "ANTHROPIC_API_KEY",
                ],
            ),
        ]


class TestApiKeyEnvVar:
    @pytest.mark.parametrize(
        ("mode", "profile_id"), [("openai", OPENAI_PROFILE_ID), ("api-key", ANTHROPIC_PROFILE_ID)]
    )
    def test_matches_the_packaged_profile(self, mode, profile_id):
        (credential,) = _packaged_profile(profile_id)["credentials"]
        assert credential["env_vars"] == [api_key_env_var(mode)]

    @pytest.mark.parametrize("mode", ["vertex", "oauth", None, "openai-builtin-profile"])
    def test_other_modes_have_nothing_to_detach(self, mode):
        assert api_key_env_var(mode) is None


class TestProfileEndpointHosts:
    def test_packaged_profiles(self):
        assert profile_endpoint_hosts() == frozenset({"api.openai.com", "api.anthropic.com"})

    def test_wildcards_are_kept_and_hosts_normalized(self, tmp_path):
        (tmp_path / "vertex.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "vertex",
                    "endpoints": [
                        {"host": "*-aiplatform.googleapis.com", "port": 443},
                        {"host": " AIPlatform.us.rep.googleapis.com "},
                    ],
                }
            )
        )
        (tmp_path / "endpointless.yaml").write_text("id: endpointless\ncredentials: []\n")
        (tmp_path / "README.md").write_text("not a profile")
        assert profile_endpoint_hosts(tmp_path) == frozenset(
            {"*-aiplatform.googleapis.com", "aiplatform.us.rep.googleapis.com"}
        )

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("id: [unclosed", "is not valid YAML"),
            ("- a list", "is not a mapping"),
            ("id: other\n", "must have its file name as its id"),
            ("endpoints: []\n", "must have its file name as its id"),
            ("id: bad\nendpoints: {host: x}\n", "endpoints must be a list"),
            ("id: bad\nendpoints: [{port: 443}]\n", "endpoint 0 has no host"),
            ("id: bad\nendpoints: [{host: '  '}]\n", "endpoint 0 has no host"),
            ("id: bad\nendpoints: [{host: a.example.com}, x]\n", "endpoint 1 has no host"),
        ],
    )
    def test_malformed_profile_fails_loudly(self, tmp_path, text, message):
        (tmp_path / "bad.yaml").write_text(text)
        with pytest.raises(ValueError, match=f"provider profile bad.yaml.*{message}"):
            profile_endpoint_hosts(tmp_path)
