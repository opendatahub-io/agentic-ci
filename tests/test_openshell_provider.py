"""Tests for OpenShell provider token rotation."""

import subprocess
from importlib.resources import files
from unittest import mock

import pytest
import yaml

from agentic_ci.backends.openshell import provider as openshell_provider
from agentic_ci.backends.openshell.provider import (
    ANTHROPIC_PROFILE_ID,
    OPENAI_PROFILE_ID,
    PROVIDER_NAME,
    VERTEX_PROFILE_ID,
    auth_mode,
    credential_fingerprint,
    delete,
    ensure_auth_mode_profile,
    ensure_profile,
    profile_endpoint_hosts,
    provider_env_vars,
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
    @pytest.mark.parametrize(
        ("cred_type", "credential_key"),
        [
            ("service_account", "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN"),
            # A --from-gcloud-adc provider holds its token under the ADC key.
            ("authorized_user", "GOOGLE_VERTEX_AI_TOKEN"),
            ("unknown", "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN"),
        ],
    )
    def test_rotate_token_calls_openshell(self, cred_type, credential_key):
        with (
            mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run,
            mock.patch(
                "agentic_ci.backends.openshell.provider._adc_credential_type",
                return_value=cred_type,
            ),
        ):
            rotate_token()

        mock_run.assert_called_once_with(
            [
                "openshell",
                "provider",
                "refresh",
                "rotate",
                "--credential-key",
                credential_key,
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
            (VERTEX_PROFILE_ID, "vertex"),
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
        ("provider_type", "mode"),
        [
            ("openai", "openai"),
            ("anthropic", "api-key"),
            ("google-vertex-ai", "vertex"),
            ("google-cloud", "vertex"),
        ],
    )
    def test_auth_mode_flags_builtin_profile_providers(self, provider_type, mode):
        # Providers from the builtin profiles bind the API host to curl (or,
        # for google-vertex-ai, to binaries agentic-ci does not choose), and
        # a google-cloud provider relies on the removed metadata emulator,
        # so they must not look reusable for the auth mode they serve.
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

    def test_packaged_vertex_profile_binds_only_agent_binaries(self):
        # OpenShell's example google-vertex-ai profile binds no binary, and
        # the version agentic-ci carried before bound curl, which would let
        # any sandbox process spend the token. Only the agent binaries here.
        profile = _packaged_profile(VERTEX_PROFILE_ID)

        assert profile["id"] == VERTEX_PROFILE_ID
        assert sorted(profile["binaries"]) == sorted(AGENT_BINARY_PATHS)
        assert not any("curl" in path for path in profile["binaries"])
        assert {e["host"] for e in profile["endpoints"]} == {
            "*-aiplatform.googleapis.com",
            "aiplatform.googleapis.com",
            "aiplatform.us.rep.googleapis.com",
            "aiplatform.eu.rep.googleapis.com",
        }
        for endpoint in profile["endpoints"]:
            assert (endpoint["port"], endpoint["protocol"], endpoint["enforcement"]) == (
                443,
                "rest",
                "enforce",
            )
        # The gateway exports refresh timings as durations; the legacy
        # *_seconds form would make ensure_profile update it on every run.
        for credential in profile["credentials"]:
            refresh = credential.get("refresh", {})
            assert not {"refresh_before_seconds", "max_lifetime_seconds"} & set(refresh)
        # --from-gcloud-adc needs an oauth2_refresh_token credential with the
        # three gcloud ADC material keys.
        adc = next(c for c in profile["credentials"] if c["name"] == "gcloud_adc_token")
        assert adc["refresh"]["strategy"] == "oauth2_refresh_token"
        assert {m["name"] for m in adc["refresh"]["material"]} >= {
            "client_id",
            "client_secret",
            "refresh_token",
        }

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
            assert ensure_profile(OPENAI_PROFILE_ID) is True

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
            assert ensure_profile(OPENAI_PROFILE_ID) is False

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
            assert ensure_profile(OPENAI_PROFILE_ID) is True

        assert mock_run.call_args_list[1].args[0][:6] == [
            "openshell",
            "provider",
            "profile",
            "update",
            OPENAI_PROFILE_ID,
            "-f",
        ]
        assert updated == [{**_packaged_profile(OPENAI_PROFILE_ID), "resource_version": 7}]

    @pytest.mark.parametrize(
        ("mode", "profile_id"),
        [
            ("api-key", ANTHROPIC_PROFILE_ID),
            ("openai", OPENAI_PROFILE_ID),
            ("vertex", VERTEX_PROFILE_ID),
        ],
    )
    @pytest.mark.parametrize("changed", [True, False])
    def test_ensure_auth_mode_profile_syncs_the_mode_profile(self, mode, profile_id, changed):
        # A reused provider composes its rule from the gateway's copy of the
        # profile, so the backend syncs it and recreates the sandbox on a change.
        with mock.patch(
            "agentic_ci.backends.openshell.provider.ensure_profile", return_value=changed
        ) as ensure:
            assert ensure_auth_mode_profile(mode) is changed

        ensure.assert_called_once_with(profile_id)

    @pytest.mark.parametrize("mode", ["oauth", None, "vertex-google-cloud-provider"])
    def test_ensure_auth_mode_profile_skips_modes_without_a_profile(self, mode):
        with mock.patch("agentic_ci.backends.openshell.provider._run") as mock_run:
            assert ensure_auth_mode_profile(mode) is False

        mock_run.assert_not_called()

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


class TestProviderEnvVars:
    @pytest.mark.parametrize(
        ("mode", "profile_id"), [("openai", OPENAI_PROFILE_ID), ("api-key", ANTHROPIC_PROFILE_ID)]
    )
    def test_matches_the_packaged_profile(self, mode, profile_id):
        (credential,) = _packaged_profile(profile_id)["credentials"]
        assert credential["env_vars"] == list(provider_env_vars(mode))

    def test_vertex_lists_the_first_variable_of_each_token_credential(self):
        # The provider stores the service account token or the gcloud ADC
        # token under the first env var of its credential; the probe must
        # see either one.
        credentials = {c["name"]: c for c in _packaged_profile(VERTEX_PROFILE_ID)["credentials"]}
        assert provider_env_vars("vertex") == (
            credentials["service_account_token"]["env_vars"][0],
            credentials["gcloud_adc_token"]["env_vars"][0],
        )

    @pytest.mark.parametrize("mode", ["oauth", None, "openai-builtin-profile", "vertex-builtin"])
    def test_other_modes_have_nothing_to_detach(self, mode):
        assert provider_env_vars(mode) == ()


class TestProfileEndpointHosts:
    def test_packaged_profiles(self):
        assert profile_endpoint_hosts() == frozenset(
            {
                "api.openai.com",
                "api.anthropic.com",
                "*-aiplatform.googleapis.com",
                "aiplatform.googleapis.com",
                "aiplatform.us.rep.googleapis.com",
                "aiplatform.eu.rep.googleapis.com",
            }
        )

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


class TestVertexProvider:
    def _setup(self, monkeypatch, cred_type, adc_path=None):
        monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
        monkeypatch.setenv("CLOUD_ML_REGION", "global")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("VERTEX_LOCATION", raising=False)
        calls = []
        with (
            mock.patch(
                "agentic_ci.backends.openshell.provider.provider_exists", return_value=False
            ),
            mock.patch(
                "agentic_ci.backends.openshell.provider.ensure_profile",
                side_effect=lambda profile_id: calls.append(("profile", profile_id)),
            ),
            mock.patch("agentic_ci.backends.openshell.provider.ensure_adc", return_value="adc"),
            mock.patch(
                "agentic_ci.backends.openshell.provider._adc_credential_type",
                return_value=cred_type,
            ),
            mock.patch("agentic_ci.backends.openshell.provider._adc_path", return_value=adc_path),
            mock.patch(
                "agentic_ci.backends.openshell.provider._run",
                side_effect=lambda args, **kwargs: calls.append(("run", args)),
            ),
        ):
            setup("vertex")
        return calls

    def test_adc_creates_provider_from_the_vendored_profile(self, monkeypatch):
        calls = self._setup(monkeypatch, "authorized_user")

        assert calls == [
            ("profile", VERTEX_PROFILE_ID),
            (
                "run",
                [
                    "openshell",
                    "provider",
                    "create",
                    "--name",
                    PROVIDER_NAME,
                    "--type",
                    VERTEX_PROFILE_ID,
                    "--from-gcloud-adc",
                    "--config",
                    "VERTEX_AI_PROJECT_ID=proj",
                    "--config",
                    "VERTEX_AI_REGION=global",
                ],
            ),
        ]

    def test_service_account_configures_vertex_token_refresh(self, monkeypatch, tmp_path):
        key = tmp_path / "sa.json"
        key.write_text(
            '{"type": "service_account", "client_email": "sa@proj.iam", "private_key": "PEM"}'
        )
        calls = self._setup(monkeypatch, "service_account", adc_path=str(key))

        assert calls[0] == ("profile", VERTEX_PROFILE_ID)
        create, configure, rotate = (args for kind, args in calls[1:])
        assert create == [
            "openshell",
            "provider",
            "create",
            "--name",
            PROVIDER_NAME,
            "--type",
            VERTEX_PROFILE_ID,
            "--credential",
            "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN=placeholder",
            "--config",
            "VERTEX_AI_PROJECT_ID=proj",
            "--config",
            "VERTEX_AI_REGION=global",
        ]
        assert configure[:6] == [
            "openshell",
            "provider",
            "refresh",
            "configure",
            "--credential-key",
            "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN",
        ]
        assert "client_email=sa@proj.iam" in configure
        assert rotate == [
            "openshell",
            "provider",
            "refresh",
            "rotate",
            "--credential-key",
            "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN",
            PROVIDER_NAME,
        ]
        # Nothing refers to the removed metadata emulator's provider type or keys.
        flat = [arg for _kind, args in calls[1:] for arg in args]
        assert "google-cloud" not in flat
        assert not any(arg.startswith(("project_id=", "service_account_email=")) for arg in flat)

    def test_vertex_token_placeholder_is_redacted_in_the_log(self):
        with (
            mock.patch("agentic_ci.backends.openshell.provider.log.detail") as detail,
            mock.patch("agentic_ci.backends.openshell.provider.subprocess.run"),
        ):
            openshell_provider._run(
                ["openshell", "private_key=SECRET", "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN=t"]
            )
        logged = detail.call_args.args[1]
        assert "SECRET" not in logged
        assert "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN=<redacted>" in logged
