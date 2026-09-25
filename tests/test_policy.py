"""Tests for policy resolution."""

import pytest

from agentic_ci import sandbox_profile
from agentic_ci.backends.openshell.policy import (
    AUTH_ENDPOINTS,
    DEFAULT_ENDPOINTS,
    EGRESS_PHASES,
    EGRESS_PRESETS,
    EgressPreset,
    build_credential_binding_patch,
    phase_endpoints,
    resolve_endpoints,
)
from agentic_ci.sandbox_profile import SandboxProfile, parse_profile

NPM = "registry.npmjs.org:443:read-only"
GOPROXY = [
    "proxy.golang.org:443:read-only",
    "sum.golang.org:443:read-only",
    "storage.googleapis.com:443:read-only",
]
RAW = "internal.example.com:443:read-only"


def _profile(*egress):
    return parse_profile({"egress": list(egress)}, source="central").profile


def _write_repo_policy(tmp_path, endpoint="repo.example.com:443:full"):
    policy_dir = tmp_path / ".agentic-ci"
    policy_dir.mkdir()
    (policy_dir / "openshell-policy.yml").write_text(f"endpoints:\n  - '{endpoint}'\n")


def test_default_endpoints_returned_when_no_flag(tmp_path):
    result = resolve_endpoints(flag_path=None, workdir=str(tmp_path))
    assert result == list(DEFAULT_ENDPOINTS)


def test_flag_file_endpoints_merged(tmp_path):
    flag_file = tmp_path / "custom.yml"
    flag_file.write_text("endpoints:\n  - 'jira.example.com:443:read-only'\n")
    result = resolve_endpoints(flag_path=str(flag_file))
    assert result == list(DEFAULT_ENDPOINTS) + ["jira.example.com:443:read-only"]


def test_flag_file_without_endpoints_key(tmp_path):
    flag_file = tmp_path / "custom.yml"
    flag_file.write_text("custom: true\n")
    result = resolve_endpoints(flag_path=str(flag_file))
    assert result == list(DEFAULT_ENDPOINTS)


def test_repo_policy_file_merged(tmp_path):
    policy_dir = tmp_path / ".agentic-ci"
    policy_dir.mkdir()
    policy_file = policy_dir / "openshell-policy.yml"
    policy_file.write_text("endpoints:\n  - 'internal.example.com:443:full'\n")
    result = resolve_endpoints(workdir=str(tmp_path))
    assert result == list(DEFAULT_ENDPOINTS) + ["internal.example.com:443:full"]


def test_flag_takes_precedence_over_repo(tmp_path):
    policy_dir = tmp_path / ".agentic-ci"
    policy_dir.mkdir()
    (policy_dir / "openshell-policy.yml").write_text(
        "endpoints:\n  - 'repo.example.com:443:full'\n"
    )
    flag_file = tmp_path / "flag.yml"
    flag_file.write_text("endpoints:\n  - 'flag.example.com:443:full'\n")
    result = resolve_endpoints(flag_path=str(flag_file), workdir=str(tmp_path))
    assert "flag.example.com:443:full" in result
    assert "repo.example.com:443:full" not in result


def test_duplicate_endpoints_deduplicated(tmp_path):
    flag_file = tmp_path / "custom.yml"
    flag_file.write_text("endpoints:\n  - 'github.com:443:full'\n")
    result = resolve_endpoints(flag_path=str(flag_file))
    assert result.count("github.com:443:full") == 1


def test_endpoints_include_vertex_ai():
    result = resolve_endpoints(auth_mode="vertex")
    assert any("aiplatform.googleapis.com" in ep for ep in result)
    assert not any("api.anthropic.com" in ep for ep in result)
    assert not any("api.openai.com" in ep for ep in result)


def test_endpoints_include_anthropic_api():
    result = resolve_endpoints(auth_mode="api-key")
    assert "api.anthropic.com:443:read-write:::allow-uninspected-credentials" in result
    assert not any("aiplatform.googleapis.com" in ep for ep in result)
    assert not any("api.openai.com" in ep for ep in result)


def test_credential_binding_patch_adds_binding_to_gcp():
    policy_get_output = {
        "scope": "sandbox",
        "sandbox": "ci",
        "version": 2,
        "policy": {
            "version": 1,
            "network_policies": {
                "ci": {
                    "endpoints": [
                        {"host": "github.com", "port": 443, "access": "full"},
                        {"host": "aiplatform.googleapis.com", "port": 443, "access": "read-write"},
                        {"host": "oauth2.googleapis.com", "port": 443, "access": "read-write"},
                    ],
                    "binaries": [{"path": "/usr/local/bin/claude"}],
                }
            },
        },
    }
    patched = build_credential_binding_patch(policy_get_output)
    assert patched is not None
    assert "scope" not in patched
    endpoints = patched["network_policies"]["ci"]["endpoints"]
    github_ep = [e for e in endpoints if e["host"] == "github.com"][0]
    assert "credential_binding" not in github_ep
    assert "allow_uninspected_credentials" not in github_ep
    gcp_ep = [e for e in endpoints if e["host"] == "aiplatform.googleapis.com"][0]
    assert gcp_ep["credential_binding"]["provider"] == "ci-gcp"
    assert gcp_ep["allow_uninspected_credentials"] is True
    oauth_ep = [e for e in endpoints if e["host"] == "oauth2.googleapis.com"][0]
    assert oauth_ep["credential_binding"]["provider"] == "ci-gcp"
    assert oauth_ep["allow_uninspected_credentials"] is True


def test_credential_binding_patch_returns_none_when_no_gcp():
    policy_get_output = {
        "scope": "sandbox",
        "policy": {
            "version": 1,
            "network_policies": {
                "ci": {
                    "endpoints": [{"host": "github.com", "port": 443, "access": "full"}],
                }
            },
        },
    }
    assert build_credential_binding_patch(policy_get_output) is None


def test_credential_binding_patch_preserves_existing_binding():
    policy_get_output = {
        "scope": "sandbox",
        "policy": {
            "version": 1,
            "network_policies": {
                "ci": {
                    "endpoints": [
                        {
                            "host": "aiplatform.googleapis.com",
                            "port": 443,
                            "credential_binding": {"provider": "other-provider"},
                        },
                    ],
                }
            },
        },
    }
    assert build_credential_binding_patch(policy_get_output) is None


def test_endpoints_include_anthropic_api_for_oauth():
    # No provider backs the OAuth token, so the endpoint carries no
    # provider credential and needs no uninspected-credentials opt-in.
    result = resolve_endpoints(auth_mode="oauth")
    assert result == list(DEFAULT_ENDPOINTS) + ["api.anthropic.com:443:read-write"]


def test_endpoints_include_openai_apis():
    result = resolve_endpoints(auth_mode="openai")
    assert result == list(DEFAULT_ENDPOINTS) + AUTH_ENDPOINTS["openai"]
    assert "api.openai.com:443:read-write:::allow-uninspected-credentials" in result
    assert "chatgpt.com:443:read-write" in result
    assert not any("aiplatform.googleapis.com" in ep for ep in result)
    assert not any("api.anthropic.com" in ep for ep in result)


def test_credentialed_provider_hosts_allow_uninspected_credentials():
    # OpenShell >= v0.0.116 rejects L4-only rules for hosts the attached
    # provider profile marks as credentialed unless the endpoint opts in.
    for auth_mode, host in (("api-key", "api.anthropic.com"), ("openai", "api.openai.com")):
        matching = [ep for ep in resolve_endpoints(auth_mode=auth_mode) if ep.startswith(host)]
        assert matching == [f"{host}:443:read-write:::allow-uninspected-credentials"]


class TestEgressPresets:
    def test_preset_names_match_the_profile_schema(self):
        assert set(EGRESS_PRESETS) == sandbox_profile.KNOWN_EGRESS_PRESETS

    def test_preset_endpoints(self):
        assert EGRESS_PRESETS["pypi"].endpoints == (
            "pypi.org:443:read-only",
            "files.pythonhosted.org:443:read-only",
        )
        assert EGRESS_PRESETS["npm"].endpoints == (NPM,)
        assert list(EGRESS_PRESETS["goproxy"].endpoints) == GOPROXY
        assert EGRESS_PRESETS["github-release-assets"].endpoints == (
            "release-assets.githubusercontent.com:443:read-only",
            "objects.githubusercontent.com:443:read-only",
            "raw.githubusercontent.com:443:read-only",
        )

    def test_every_preset_is_read_only_on_443_in_every_phase(self):
        for preset in EGRESS_PRESETS.values():
            assert preset.phases == frozenset(EGRESS_PHASES)
            for endpoint in preset.endpoints:
                assert endpoint.endswith(":443:read-only")

    def test_presets_cannot_be_changed(self):
        with pytest.raises(TypeError):
            EGRESS_PRESETS["npm"] = EgressPreset(endpoints=("evil.example.com:443:full",))


class TestResolveEndpointsWithProfile:
    def test_no_profile_is_unchanged(self, tmp_path, capsys):
        _write_repo_policy(tmp_path)
        result = resolve_endpoints(workdir=str(tmp_path), auth_mode="openai", profile=None)
        assert result == [
            *DEFAULT_ENDPOINTS,
            *AUTH_ENDPOINTS["openai"],
            "repo.example.com:443:full",
        ]
        assert "Policy source: repo (" in capsys.readouterr().out

    def test_repo_policy_file_is_ignored_with_a_profile(self, tmp_path, capsys):
        _write_repo_policy(tmp_path)
        result = resolve_endpoints(workdir=str(tmp_path), profile=SandboxProfile())
        assert result == list(DEFAULT_ENDPOINTS)
        assert (
            "Policy source: sandbox profile (repo policy file ignored)" in capsys.readouterr().out
        )

    def test_source_without_a_repo_policy_file(self, tmp_path, capsys):
        resolve_endpoints(workdir=str(tmp_path), profile=SandboxProfile())
        out = capsys.readouterr().out
        assert "Policy source: sandbox profile\n" in out

    def test_policy_flag_still_applies_with_a_profile(self, tmp_path, capsys):
        _write_repo_policy(tmp_path)
        flag_file = tmp_path / "flag.yml"
        flag_file.write_text("endpoints:\n  - 'flag.example.com:443:full'\n")
        result = resolve_endpoints(
            flag_path=str(flag_file), workdir=str(tmp_path), profile=_profile("npm")
        )
        assert result == [*DEFAULT_ENDPOINTS, NPM, "flag.example.com:443:full"]
        assert "repo.example.com:443:full" not in result
        assert "and sandbox profile" in capsys.readouterr().out

    def test_agent_presets_and_raw_egress_follow_defaults_and_auth(self, tmp_path):
        result = resolve_endpoints(
            workdir=str(tmp_path), auth_mode="openai", profile=_profile("goproxy", "npm", RAW)
        )
        assert result == [
            *DEFAULT_ENDPOINTS,
            *AUTH_ENDPOINTS["openai"],
            *GOPROXY,
            NPM,
            RAW,
        ]

    def test_endpoints_are_deduplicated(self, tmp_path):
        # pypi is already a default, and the raw endpoint repeats a preset.
        result = resolve_endpoints(workdir=str(tmp_path), profile=_profile("pypi", "npm", NPM))
        assert result == [*DEFAULT_ENDPOINTS, NPM]

    def test_presets_closed_in_the_agent_phase_are_left_out(self, tmp_path, monkeypatch):
        setup_only = EgressPreset(
            endpoints=("setup.example.com:443:read-only",), phases=frozenset({"setup"})
        )
        monkeypatch.setattr(
            "agentic_ci.backends.openshell.policy.EGRESS_PRESETS", {"npm": setup_only}
        )
        result = resolve_endpoints(workdir=str(tmp_path), profile=_profile("npm"))
        assert result == list(DEFAULT_ENDPOINTS)


class TestPhaseEndpoints:
    @pytest.mark.parametrize("phase", ["setup", "validate"])
    def test_presets_then_raw_egress(self, phase):
        profile = _profile("npm", "goproxy", RAW)
        assert phase_endpoints(profile, phase) == [NPM, *GOPROXY, RAW]

    @pytest.mark.parametrize("phase", ["setup", "validate"])
    @pytest.mark.parametrize("auth_mode", sorted(AUTH_ENDPOINTS))
    def test_never_includes_defaults_auth_or_otel(self, phase, auth_mode):
        result = phase_endpoints(_profile("npm", "github-release-assets"), phase)
        assert not set(result) & set(DEFAULT_ENDPOINTS)
        assert not set(result) & set(AUTH_ENDPOINTS[auth_mode])
        assert not any("host.openshell.internal" in ep for ep in result)

    def test_pypi_preset_repeats_default_hosts_for_the_shim(self):
        # The defaults are bound to the agent binaries only, so the shim needs
        # the pypi hosts from the preset.
        assert phase_endpoints(_profile("pypi"), "setup") == list(EGRESS_PRESETS["pypi"].endpoints)

    def test_empty_profile_opens_nothing(self):
        assert phase_endpoints(SandboxProfile(), "setup") == []

    def test_phase_filter(self, monkeypatch):
        presets = {
            "npm": EgressPreset(endpoints=(NPM,), phases=frozenset({"setup"})),
            "goproxy": EgressPreset(endpoints=tuple(GOPROXY), phases=frozenset({"validate"})),
        }
        monkeypatch.setattr("agentic_ci.backends.openshell.policy.EGRESS_PRESETS", presets)
        profile = _profile("npm", "goproxy", RAW)
        assert phase_endpoints(profile, "setup") == [NPM, RAW]
        assert phase_endpoints(profile, "validate") == [*GOPROXY, RAW]

    @pytest.mark.parametrize("phase", ["agent", "install", ""])
    def test_only_shim_phases(self, phase):
        with pytest.raises(ValueError, match="phase must be one of setup, validate"):
            phase_endpoints(SandboxProfile(), phase)

    def test_unknown_preset_on_a_directly_built_profile_is_skipped(self, capsys):
        profile = SandboxProfile(egress=("npm", "not-a-preset"))
        assert phase_endpoints(profile, "setup") == [NPM]
        out = capsys.readouterr().out
        assert "1 unknown egress preset(s)" in out
        assert "not-a-preset" not in out

    @pytest.mark.parametrize(
        "endpoint",
        [
            "internal.example.com:443:read-write:::allow-uninspected-credentials",
            "internal.example.com:443:read-write:rest:enforce:request-body-credential-rewrite",
            "internal.example.com:443:full:websocket::allowed-ip=10.0.0.1,"
            "websocket-credential-rewrite",
        ],
    )
    def test_raw_egress_with_a_credential_option_is_not_opened(self, endpoint, capsys):
        profile = _profile("npm", endpoint)
        assert phase_endpoints(profile, "setup") == [NPM]
        out = capsys.readouterr().out
        assert "1 egress endpoint(s) not opened in the setup phase" in out
        assert "internal.example.com" not in out

    @pytest.mark.parametrize(
        "endpoint",
        [
            "api.openai.com:443:read-only",
            "API.OpenAI.com:443:read-only",
            "chatgpt.com:443:read-write",
            "api.anthropic.com:443:read-only",
            "oauth2.googleapis.com:443:read-only",
            "us.aiplatform.googleapis.com:443:read-only",
            "*.googleapis.com:443:read-only",
            "*.openai.com:443:read-only",
            "host.openshell.internal:4318:read-write",
        ],
    )
    def test_raw_egress_to_auth_llm_or_otel_hosts_is_not_opened(self, endpoint, capsys):
        profile = _profile(endpoint, RAW)
        assert phase_endpoints(profile, "validate") == [RAW]
        out = capsys.readouterr().out
        assert "1 egress endpoint(s) not opened in the validate phase" in out
        assert endpoint.split(":")[0] not in out

    def test_unrelated_hosts_and_allowed_ip_stay(self, capsys):
        storage = "storage.googleapis.com:443:read-only"
        pinned = "internal.example.com:443:read-only:::allowed-ip=10.0.0.0/8"
        assert phase_endpoints(_profile(storage, pinned), "setup") == [storage, pinned]
        assert "not opened" not in capsys.readouterr().out

    def test_agent_phase_keeps_raw_credential_egress(self, tmp_path):
        endpoint = "api.example.com:443:read-write:::allow-uninspected-credentials"
        result = resolve_endpoints(workdir=str(tmp_path), profile=_profile(endpoint))
        assert endpoint in result
