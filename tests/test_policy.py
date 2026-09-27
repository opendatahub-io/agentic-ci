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

NPM = "registry.npmjs.org:443:read-only:rest:enforce"
GOPROXY = [
    "proxy.golang.org:443:read-only:rest:enforce",
    "sum.golang.org:443:read-only:rest:enforce",
    "storage.googleapis.com:443:read-only:rest:enforce",
]
PYPI = ["pypi.org:443:read-only:rest:enforce", "files.pythonhosted.org:443:read-only:rest:enforce"]
RAW = "internal.example.com:443:read-only"
# Other endpoints on 443 that overlap the goproxy preset's storage.googleapis.com.
PRESET_HOST_TWINS = [
    "storage.googleapis.com:443:full",
    "storage.googleapis.com:443:read-write",
    "storage.googleapis.com:443:read-only",
    "storage.googleapis.com:443:read-only:rest:audit",
    "Storage.GoogleAPIs.com:443:full",
    "*.googleapis.com:443:full",
    "storage.googleapis.com:0443:full",
    "storage.googleapis.com:00443:read-write",
]


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
        assert list(EGRESS_PRESETS["pypi"].endpoints) == PYPI
        assert EGRESS_PRESETS["npm"].endpoints == (NPM,)
        assert list(EGRESS_PRESETS["goproxy"].endpoints) == GOPROXY
        assert EGRESS_PRESETS["github-release-assets"].endpoints == (
            "release-assets.githubusercontent.com:443:read-only:rest:enforce",
            "objects.githubusercontent.com:443:read-only:rest:enforce",
            "raw.githubusercontent.com:443:read-only:rest:enforce",
        )

    def test_every_preset_is_enforced_read_only_at_l7_on_443_in_every_phase(self):
        # Without a protocol read-only blocks nothing (an L4 CONNECT tunnel),
        # and without enforce OpenShell only audits a denied request.
        for preset in EGRESS_PRESETS.values():
            assert preset.phases == frozenset(EGRESS_PHASES)
            for endpoint in preset.endpoints:
                host, port, access, protocol, enforcement = endpoint.split(":")
                assert (port, access, protocol, enforcement) == (
                    "443",
                    "read-only",
                    "rest",
                    "enforce",
                )
                assert sandbox_profile.is_raw_endpoint(endpoint)

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
        # The raw endpoint repeats a preset.
        result = resolve_endpoints(workdir=str(tmp_path), profile=_profile("npm", NPM))
        assert result == [*DEFAULT_ENDPOINTS, NPM]

    def test_pypi_preset_replaces_the_l4_defaults_in_place(self, tmp_path, capsys):
        # The defaults hold the pypi hosts as L4 read-only; the preset's L7
        # endpoints take their place (before the auth endpoints), so each
        # host has one L7 endpoint.
        result = resolve_endpoints(
            workdir=str(tmp_path), auth_mode="openai", profile=_profile("pypi")
        )
        expected = [ep for ep in DEFAULT_ENDPOINTS if not ep.startswith(("pypi.", "files."))]
        assert result == [*expected, *PYPI, *AUTH_ENDPOINTS["openai"]]
        # Replacing a built-in default is expected, so it is not warned about.
        assert "replaced" not in capsys.readouterr().out
        assert DEFAULT_ENDPOINTS[-2:] == [
            "pypi.org:443:read-only",
            "files.pythonhosted.org:443:read-only",
        ]
        assert "pypi.org:443:read-only" not in result
        assert "files.pythonhosted.org:443:read-only" not in result

    def test_no_pypi_preset_keeps_the_l4_defaults(self, tmp_path):
        result = resolve_endpoints(workdir=str(tmp_path), profile=_profile("npm"))
        assert result == [*DEFAULT_ENDPOINTS, NPM]

    def test_l4_twin_of_a_preset_from_raw_or_flag_is_replaced(self, tmp_path):
        flag_file = tmp_path / "flag.yml"
        flag_file.write_text("endpoints:\n  - 'proxy.golang.org:443:read-only'\n")
        result = resolve_endpoints(
            flag_path=str(flag_file),
            workdir=str(tmp_path),
            profile=_profile("registry.npmjs.org:443:read-only", "npm", "goproxy"),
        )
        assert result == [*DEFAULT_ENDPOINTS, NPM, *GOPROXY]

    @pytest.mark.parametrize("twin", PRESET_HOST_TWINS)
    def test_any_other_endpoint_for_a_preset_host_gives_way(self, tmp_path, twin, capsys):
        result = resolve_endpoints(workdir=str(tmp_path), profile=_profile("goproxy", twin, RAW))
        assert result == [*DEFAULT_ENDPOINTS, *GOPROXY, RAW]
        out = capsys.readouterr().out
        assert "WARNING: 1 egress endpoint(s) for an egress preset host replaced" in out
        assert "googleapis" not in out

    def test_twin_before_its_preset_takes_the_preset_in_its_place(self, tmp_path):
        flag_file = tmp_path / "flag.yml"
        flag_file.write_text("endpoints:\n  - 'registry.npmjs.org:443:full'\n")
        result = resolve_endpoints(
            flag_path=str(flag_file),
            workdir=str(tmp_path),
            profile=_profile("*.npmjs.org:443:read-write", "npm"),
        )
        assert result == [*DEFAULT_ENDPOINTS, NPM]

    def test_a_wildcard_takes_every_preset_host_it_covers(self, tmp_path):
        result = resolve_endpoints(
            workdir=str(tmp_path),
            profile=_profile("github-release-assets", "*.githubusercontent.com:443:full"),
        )
        assert result == [*DEFAULT_ENDPOINTS, *EGRESS_PRESETS["github-release-assets"].endpoints]

    @pytest.mark.parametrize(
        "twin",
        [
            "registry.npmjs.org:0443:full",
            "registry.npmjs.org:+443:full",
            " registry.npmjs.org : 443 :full",
        ],
    )
    def test_policy_file_twin_on_443_spelled_otherwise_is_replaced(self, tmp_path, twin):
        # OpenShell trims each segment and reads the port as a number, so
        # these all open port 443 and must give way to the preset.
        flag_file = tmp_path / "flag.yml"
        flag_file.write_text(f"endpoints:\n  - '{twin}'\n")
        result = resolve_endpoints(
            flag_path=str(flag_file), workdir=str(tmp_path), profile=_profile("npm")
        )
        assert result == [*DEFAULT_ENDPOINTS, NPM]

    @pytest.mark.parametrize("port", ["4430", "44_3", "443x", ""])
    def test_policy_file_endpoint_on_another_or_bad_port_is_kept(self, tmp_path, port):
        endpoint = f"registry.npmjs.org:{port}:full"
        flag_file = tmp_path / "flag.yml"
        flag_file.write_text(f"endpoints:\n  - '{endpoint}'\n")
        result = resolve_endpoints(
            flag_path=str(flag_file), workdir=str(tmp_path), profile=_profile("npm")
        )
        assert result == [*DEFAULT_ENDPOINTS, NPM, endpoint]

    def test_preset_host_on_another_port_is_kept(self, tmp_path, capsys):
        other_port = "registry.npmjs.org:8443:full"
        result = resolve_endpoints(workdir=str(tmp_path), profile=_profile("npm", other_port))
        assert result == [*DEFAULT_ENDPOINTS, NPM, other_port]
        assert "replaced" not in capsys.readouterr().out

    def test_a_preset_string_without_its_preset_is_a_plain_raw_endpoint(self, tmp_path):
        # Only the presets the profile opens take over their hosts.
        result = resolve_endpoints(workdir=str(tmp_path), profile=_profile(PYPI[0]))
        assert result == [*DEFAULT_ENDPOINTS, PYPI[0]]

    def test_no_profile_keeps_the_l4_defaults(self, tmp_path):
        result = resolve_endpoints(workdir=str(tmp_path))
        assert result == list(DEFAULT_ENDPOINTS)
        assert all(len(ep.split(":")) == 3 for ep in DEFAULT_ENDPOINTS)

    def test_no_profile_repo_file_with_a_preset_string_changes_nothing_else(self, tmp_path):
        # The repo file is untrusted: naming a preset's L7 endpoint must not
        # drop or reorder the defaults when there is no profile.
        _write_repo_policy(tmp_path, PYPI[0])
        result = resolve_endpoints(workdir=str(tmp_path), auth_mode="openai")
        assert result == [*DEFAULT_ENDPOINTS, *AUTH_ENDPOINTS["openai"], PYPI[0]]

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
        assert phase_endpoints(_profile("pypi"), "setup") == PYPI

    def test_raw_l4_twin_of_a_preset_is_replaced(self):
        profile = _profile("registry.npmjs.org:443:read-only", "npm", RAW)
        assert phase_endpoints(profile, "setup") == [NPM, RAW]

    @pytest.mark.parametrize("phase", ["setup", "validate"])
    @pytest.mark.parametrize("twin", PRESET_HOST_TWINS)
    def test_any_other_endpoint_for_a_preset_host_gives_way(self, phase, twin, capsys):
        # The same outcome as the agent phase (resolve_endpoints): one
        # endpoint per preset host, the preset's.
        assert phase_endpoints(_profile("goproxy", twin, RAW), phase) == [*GOPROXY, RAW]
        out = capsys.readouterr().out
        assert "WARNING: 1 egress endpoint(s) for an egress preset host replaced" in out
        assert "not opened" not in out

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
