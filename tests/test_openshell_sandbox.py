"""Tests for OpenShell sandbox lifecycle commands."""

import copy
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import yaml

import agentic_ci.backends.openshell as openshell_backend
from agentic_ci.backends import create_backend
from agentic_ci.backends.openshell import sandbox
from agentic_ci.backends.openshell.policy import (
    EGRESS_PRESETS,
    build_credential_binding_patch,
    phase_endpoints,
)
from agentic_ci.backends.openshell.provider import PROVIDER_NAME
from agentic_ci.sandbox_profile import (
    Resources,
    SandboxProfile,
    is_raw_endpoint,
    parse_profile,
    profile_hash,
)

# ``openshell policy get --base -o json`` from the September 2026 spike
# (OpenShell v0.0.116-rhaiv.1, openai auth), trimmed to three agent rules.
POLICY_GET_BASE = json.loads(
    (Path(__file__).parent / "fixtures" / "openshell_policy_get_base.json").read_text()
)
SHIM = sandbox.SANDBOX_SETUP_SHIM
PARKED = sandbox.PARKED_BINARY_PREFIX
NPM = "registry.npmjs.org:443:read-only:rest:enforce"
GOPROXY = "proxy.golang.org:443:read-only:rest:enforce"
_L7_READ_ONLY = {"access": "read-only", "protocol": "rest", "enforcement": "enforce"}


@pytest.fixture
def created():
    """Create a sandbox and return the argv passed to OpenShell."""

    def _create(**kwargs):
        with (
            mock.patch.object(sandbox, "_run") as run,
            mock.patch.object(sandbox, "_apply_policy"),
        ):
            sandbox.create(**kwargs)
        return run.call_args_list[0].args[0]

    return _create


class TestCreateResourceFlags:
    def test_no_resource_flags_by_default(self, created):
        assert created() == [
            "openshell",
            "sandbox",
            "create",
            "--name",
            sandbox.SANDBOX_NAME,
            "--no-tty",
            "--no-auto-providers",
            "--provider",
            PROVIDER_NAME,
            "--detach",
            "--",
            "sleep",
            "infinity",
        ]

    def test_resource_limits_are_passed(self, created):
        args = created(memory="8Gi", cpu="2.5", gpu=1)
        assert args[args.index("--memory") + 1] == "8Gi"
        assert args[args.index("--cpu") + 1] == "2.5"
        assert args[args.index("--gpu") + 1] == "1"

    def test_resource_flags_precede_the_command_terminator(self, created):
        args = created(memory="8Gi", cpu="4", gpu=1)
        terminator = args.index("--")
        for flag in ("--memory", "--cpu", "--gpu"):
            assert args.index(flag) < terminator

    def test_resource_flags_combine_with_image_and_approval_mode(self, created):
        args = created(image="quay.io/example/sandbox:1", approval_mode="ask", memory="8Gi")
        assert args[args.index("--from") + 1] == "quay.io/example/sandbox:1"
        assert args[args.index("--approval-mode") + 1] == "ask"
        assert args[args.index("--memory") + 1] == "8Gi"

    @pytest.mark.parametrize("value", [None, "", 0])
    def test_falsy_values_leave_the_flag_off(self, created, value):
        args = created(memory=value, cpu=value, gpu=value)
        assert "--memory" not in args
        assert "--cpu" not in args
        assert "--gpu" not in args


class TestBackendPassesResourcesThrough:
    def test_create_backend_accepts_resource_kwargs(self):
        backend = create_backend(
            "openshell",
            harness=mock.Mock(),
            memory="8Gi",
            cpu="4",
            gpu=1,
        )
        assert (backend.memory, backend.cpu, backend.gpu) == ("8Gi", "4", 1)

    def test_defaults_to_no_resource_request(self):
        backend = create_backend("openshell", harness=mock.Mock())
        assert (backend.memory, backend.cpu, backend.gpu) == (None, None, None)


def test_create_attaches_no_provider_for_oauth():
    with (
        mock.patch.object(sandbox, "_run") as run,
        mock.patch.object(sandbox, "_apply_policy"),
    ):
        sandbox.create(auth_mode="oauth")

    create_args = run.call_args_list[0].args[0]
    assert "--no-auto-providers" in create_args
    assert "--provider" not in create_args


def test_create_uses_detached_persistent_main_process():
    with (
        mock.patch.object(sandbox, "_run") as run,
        mock.patch.object(sandbox, "_apply_policy"),
    ):
        sandbox.create(image="codex-sandbox:latest")

    create_args = run.call_args.args[0]
    assert "--detach" in create_args
    assert create_args[-3:] == ["--", "sleep", "infinity"]


def test_apply_policy_allows_hummingbird_binary_aliases():
    with (
        mock.patch.object(sandbox, "resolve_endpoints", return_value=["github.com:443:full"]),
        mock.patch.object(sandbox, "_apply_credential_bindings"),
        mock.patch.object(sandbox, "_run") as run,
    ):
        sandbox._apply_policy(policy_path=None)

    update_args = run.call_args.args[0]
    binary_paths = [
        update_args[index + 1]
        for index, argument in enumerate(update_args)
        if argument == "--binary"
    ]
    assert binary_paths == list(sandbox.AGENT_BINARY_PATHS)


@pytest.mark.parametrize(
    ("auth_mode", "binds"),
    [(None, True), ("vertex", True), ("oauth", False)],
)
def test_apply_policy_binds_credentials_only_with_a_provider(auth_mode, binds):
    """A provider-less sandbox has no provider for GCP hosts to bind to."""
    with (
        mock.patch.object(
            sandbox, "resolve_endpoints", return_value=["oauth2.googleapis.com:443:read-write"]
        ),
        mock.patch.object(sandbox, "_apply_credential_bindings") as apply_bindings,
        mock.patch.object(sandbox, "_run"),
    ):
        sandbox._apply_policy(policy_path=None, auth_mode=auth_mode)

    assert apply_bindings.called is binds


class TestExistingSandboxKeepsItsAllocation:
    """Resource limits are fixed at creation, so reuse cannot apply new ones."""

    def test_a_reused_sandbox_says_the_request_was_not_applied(self):
        backend = create_backend("openshell", harness=mock.Mock(), memory="8Gi", gpu=1)
        with mock.patch("agentic_ci.backends.openshell.log.info") as logged:
            backend._warn_unapplied_resources()

        warning = logged.call_args.args[0]
        assert "memory=8Gi" in warning and "gpu=1" in warning
        assert "Delete it" in warning

    def test_nothing_is_said_when_nothing_was_requested(self):
        backend = create_backend("openshell", harness=mock.Mock())
        with mock.patch("agentic_ci.backends.openshell.log.info") as logged:
            backend._warn_unapplied_resources()

        logged.assert_not_called()


class TestSandboxProfileResources:
    """A profile's ``resources`` size the sandbox unless the caller passed a value."""

    def _backend(self, resources, **kwargs):
        profile = SandboxProfile(resources=resources)
        with mock.patch("agentic_ci.backends.openshell.log.info") as logged:
            backend = create_backend(
                "openshell", harness=mock.Mock(), sandbox_profile=profile, **kwargs
            )
        return backend, logged

    def test_profile_resources_are_applied(self):
        backend, logged = self._backend(Resources(memory="8Gi", cpu="4", gpu=1))
        assert (backend.memory, backend.cpu, backend.gpu) == ("8Gi", "4", 1)
        logged.assert_called_once_with(
            "Sandbox resources: memory=8Gi (sandbox profile), cpu=4 (sandbox profile), "
            "gpu=1 (sandbox profile)"
        )

    def test_explicit_kwargs_win(self):
        backend, logged = self._backend(Resources(memory="8Gi", cpu="4"), memory="16Gi")
        assert (backend.memory, backend.cpu, backend.gpu) == ("16Gi", "4", None)
        logged.assert_called_once_with(
            "Sandbox resources: memory=16Gi (explicit), cpu=4 (sandbox profile)"
        )

    def test_profile_without_resources_changes_nothing(self):
        backend, logged = self._backend(None, cpu="2")
        assert (backend.memory, backend.cpu, backend.gpu) == (None, "2", None)
        logged.assert_not_called()

    def test_no_profile_logs_nothing(self):
        with mock.patch("agentic_ci.backends.openshell.log.info") as logged:
            backend = create_backend("openshell", harness=mock.Mock(), memory="8Gi")
        assert backend.sandbox_profile is None
        assert backend.memory == "8Gi"
        logged.assert_not_called()

    @pytest.mark.parametrize("empty", ["", 0])
    def test_falsy_explicit_value_does_not_hide_the_profile(self, empty):
        """sandbox.create drops a falsy limit, so it must not count as explicit."""
        backend, logged = self._backend(Resources(memory="8Gi", gpu=1), memory=empty, gpu=empty)
        assert (backend.memory, backend.gpu) == ("8Gi", 1)
        logged.assert_called_once_with(
            "Sandbox resources: memory=8Gi (sandbox profile), gpu=1 (sandbox profile)"
        )

    def test_profile_gpu_zero_is_not_logged_as_applied(self):
        backend, logged = self._backend(Resources(memory="8Gi", gpu=0))
        assert backend.gpu is None
        logged.assert_called_once_with("Sandbox resources: memory=8Gi (sandbox profile)")

    def _setup(self, backend, tmp_path, monkeypatch, *, exists):
        monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(tmp_path / "state.json"))
        backend.workdir = str(tmp_path)
        backend.harness.auth_mode_for_env.return_value = "vertex"
        backend.harness.name = "claude-code"
        openshell = "agentic_ci.backends.openshell"
        identity = openshell_backend._sandbox_identity(
            "claude-code", backend.image, "vertex", backend.sandbox_profile
        )
        with (
            mock.patch(f"{openshell}.provider") as provider,
            mock.patch(f"{openshell}.gateway.is_running", return_value=True),
            mock.patch(f"{openshell}.sandbox.exists", return_value=exists),
            mock.patch(f"{openshell}.sandbox.create") as create,
            mock.patch(f"{openshell}.sandbox.delete") as delete,
            mock.patch(f"{openshell}.sandbox.upload"),
            mock.patch(f"{openshell}._load_sandbox_identity", return_value=identity),
            mock.patch(f"{openshell}.sandbox.apply_phase_policy"),
            mock.patch(f"{openshell}.log.info") as logged,
            mock.patch.object(backend, "_run_setup_steps"),
            mock.patch.object(backend, "_upload_sandbox_config"),
        ):
            provider.provider_exists.return_value = exists
            provider.auth_mode.return_value = "vertex"
            backend.setup()
        return create, delete, logged

    def test_profile_resources_reach_sandbox_create(self, tmp_path, monkeypatch):
        backend, _ = self._backend(Resources(memory="8Gi", gpu=1))
        create, _, _ = self._setup(backend, tmp_path, monkeypatch, exists=False)
        assert create.call_args.kwargs["memory"] == "8Gi"
        assert create.call_args.kwargs["cpu"] is None
        assert create.call_args.kwargs["gpu"] == 1

    def test_reused_sandbox_does_not_warn_about_profile_resources(self, tmp_path, monkeypatch):
        # The profile hash is part of the identity, so a reused sandbox was
        # created with these resources.
        backend, _ = self._backend(Resources(memory="8Gi", gpu=1))
        create, delete, logged = self._setup(backend, tmp_path, monkeypatch, exists=True)
        create.assert_not_called()
        delete.assert_not_called()
        assert not any("not applied" in c.args[0] for c in logged.call_args_list)

    def test_reused_sandbox_warns_only_about_explicit_resources(self, tmp_path, monkeypatch):
        backend, _ = self._backend(Resources(memory="8Gi", gpu=1), cpu="2")
        create, delete, logged = self._setup(backend, tmp_path, monkeypatch, exists=True)
        create.assert_not_called()
        warnings = [c.args[0] for c in logged.call_args_list if "not applied" in c.args[0]]
        assert len(warnings) == 1
        assert "cpu=2" in warnings[0] and "Delete it" in warnings[0]
        assert "memory" not in warnings[0] and "gpu" not in warnings[0]


def _base(**policy_changes):
    """A deep copy of the fixture with top-level ``.policy`` fields replaced."""
    output = copy.deepcopy(POLICY_GET_BASE)
    output["policy"].update(policy_changes)
    return output


def _all_binaries(policy):
    return [b["path"] for rule in policy["network_policies"].values() for b in rule["binaries"]]


def _phase_rules(policy):
    return {
        name: rule
        for name, rule in policy["network_policies"].items()
        if name.startswith(sandbox.PHASE_RULE_PREFIX)
    }


class TestBuildPhasePolicy:
    def test_adds_one_shim_rule_per_endpoint(self):
        policy = sandbox.build_phase_policy(POLICY_GET_BASE, [NPM, GOPROXY])
        assert _phase_rules(policy) == {
            "agentic_ci_phase_0": {
                "name": "agentic_ci_phase_0",
                "endpoints": [{"host": "registry.npmjs.org", "port": 443, **_L7_READ_ONLY}],
                "binaries": [{"path": SHIM}],
            },
            "agentic_ci_phase_1": {
                "name": "agentic_ci_phase_1",
                "endpoints": [{"host": "proxy.golang.org", "port": 443, **_L7_READ_ONLY}],
                "binaries": [{"path": SHIM}],
            },
        }

    def test_every_preset_endpoint_is_an_enforced_l7_read_only_shim_rule(self):
        profile = parse_profile({"egress": sorted(EGRESS_PRESETS)}, source="central").profile
        endpoints = phase_endpoints(profile, "setup")
        policy = sandbox.build_phase_policy(POLICY_GET_BASE, endpoints, park_agent=True)
        rules = list(_phase_rules(policy).values())
        assert len(rules) == len(endpoints) == 9
        for rule, spec in zip(rules, endpoints, strict=True):
            assert rule["endpoints"] == [{"host": spec.split(":")[0], "port": 443, **_L7_READ_ONLY}]
            assert rule["binaries"] == [{"path": SHIM}]

    @pytest.mark.parametrize(
        "twin", ["registry.npmjs.org:443:full", "registry.npmjs.org:443:read-only:rest:audit"]
    )
    def test_a_raw_twin_of_a_preset_host_gets_no_rule_of_its_own(self, twin):
        # Rules are ORed and an audit rule can shadow the preset's, so a twin
        # must not reach the phase policy (agentic_ci_phase_10 would also sort
        # before agentic_ci_phase_4).
        raw = [f"raw{i}.example.com:443:read-only" for i in range(10)]
        profile = parse_profile({"egress": ["npm", *raw, twin]}, source="central").profile
        endpoints = phase_endpoints(profile, "setup")
        assert endpoints == [NPM, *raw]
        policy = sandbox.build_phase_policy(POLICY_GET_BASE, endpoints, park_agent=True)
        npm_rules = [
            rule
            for rule in _phase_rules(policy).values()
            for ep in rule["endpoints"]
            if ep["host"] == "registry.npmjs.org"
        ]
        assert npm_rules == [
            {
                "name": "agentic_ci_phase_0",
                "endpoints": [{"host": "registry.npmjs.org", "port": 443, **_L7_READ_ONLY}],
                "binaries": [{"path": SHIM}],
            }
        ]

    def test_returns_the_raw_policy_and_leaves_agent_rules_alone(self):
        policy = sandbox.build_phase_policy(POLICY_GET_BASE, [NPM])
        assert "scope" not in policy and "hash" not in policy
        base_rules = POLICY_GET_BASE["policy"]["network_policies"]
        for name, rule in base_rules.items():
            assert policy["network_policies"][name] == rule
        assert list(policy["network_policies"])[: len(base_rules)] == list(base_rules)

    def test_does_not_modify_its_input(self):
        before = copy.deepcopy(POLICY_GET_BASE)
        sandbox.build_phase_policy(POLICY_GET_BASE, [NPM])
        assert POLICY_GET_BASE == before

    def test_preserves_static_fields(self):
        process = {"run_as_user": "sandbox", "run_as_group": "sandbox"}
        output = _base(process=process)
        policy = sandbox.build_phase_policy(output, [NPM])
        for key in ("version", "filesystem_policy", "landlock"):
            assert policy[key] == POLICY_GET_BASE["policy"][key]
        assert policy["process"] == process
        assert set(policy) == set(output["policy"])

    def test_strips_old_phase_rules(self):
        setup = sandbox.build_phase_policy(POLICY_GET_BASE, [NPM, GOPROXY])
        validate = sandbox.build_phase_policy({"policy": setup}, [GOPROXY])
        assert [r["endpoints"][0]["host"] for r in _phase_rules(validate).values()] == [
            "proxy.golang.org"
        ]
        agent = sandbox.build_phase_policy({"policy": setup}, [])
        assert agent == POLICY_GET_BASE["policy"]

    def test_drops_prefixed_rules_even_when_bound_to_other_binaries(self):
        output = _base()
        output["policy"]["network_policies"]["agentic_ci_phase_7"] = {
            "name": "agentic_ci_phase_7",
            "endpoints": [{"host": "example.com", "port": 443, "access": "full"}],
            "binaries": [{"path": SHIM}, {"path": "/usr/local/bin/codex"}],
        }
        policy = sandbox.build_phase_policy(output, [])
        assert policy == POLICY_GET_BASE["policy"]

    def test_strips_the_shim_from_every_rule(self):
        output = _base()
        rules = output["policy"]["network_policies"]
        rules["allow_pypi_org_443"]["binaries"].append({"path": SHIM})
        rules["shim_only"] = {
            "name": "shim_only",
            "endpoints": [{"host": "example.com", "port": 443, "access": "full"}],
            "binaries": [{"path": SHIM}],
        }
        policy = sandbox.build_phase_policy(output, [])
        assert SHIM not in _all_binaries(policy)
        assert "shim_only" not in policy["network_policies"]
        assert (
            policy["network_policies"]["allow_pypi_org_443"]
            == POLICY_GET_BASE["policy"]["network_policies"]["allow_pypi_org_443"]
        )

    def test_never_leaves_a_rule_with_empty_binaries(self):
        output = _base()
        rules = output["policy"]["network_policies"]
        rules["shim_twice"] = {
            "name": "shim_twice",
            "endpoints": [{"host": "example.com", "port": 443, "access": "full"}],
            "binaries": [{"path": SHIM}, {"path": SHIM}],
        }
        policy = sandbox.build_phase_policy(output, [NPM])
        assert "shim_twice" not in policy["network_policies"]
        for rule in policy["network_policies"].values():
            assert rule["binaries"]

    def test_leaves_a_rule_that_already_allowed_any_binary(self):
        output = _base()
        any_binary = {
            "name": "any_binary",
            "endpoints": [{"host": "example.com", "port": 443, "access": "full"}],
            "binaries": [],
        }
        output["policy"]["network_policies"]["any_binary"] = any_binary
        policy = sandbox.build_phase_policy(output, [])
        assert policy["network_policies"]["any_binary"] == any_binary

    def test_preserves_credential_bindings(self):
        output = _base()
        output["policy"]["network_policies"]["allow_oauth2_googleapis_com_443"] = {
            "name": "allow_oauth2_googleapis_com_443",
            "endpoints": [{"host": "oauth2.googleapis.com", "port": 443, "access": "read-write"}],
            "binaries": [{"path": "/usr/local/bin/claude"}],
        }
        bound = {"policy": build_credential_binding_patch(output)}
        policy = sandbox.build_phase_policy(bound, [NPM])
        endpoint = policy["network_policies"]["allow_oauth2_googleapis_com_443"]["endpoints"][0]
        assert endpoint["credential_binding"] == {"provider": PROVIDER_NAME}
        assert endpoint["allow_uninspected_credentials"] is True
        back = sandbox.build_phase_policy({"policy": policy}, [])
        assert back == bound["policy"]

    def test_is_idempotent(self):
        once = sandbox.build_phase_policy(POLICY_GET_BASE, [NPM, GOPROXY])
        twice = sandbox.build_phase_policy({"policy": once}, [NPM, GOPROXY])
        assert twice == once

    def test_shim_phases_park_every_agent_binary(self):
        policy = sandbox.build_phase_policy(POLICY_GET_BASE, [NPM], park_agent=True)
        base_rules = POLICY_GET_BASE["policy"]["network_policies"]
        for name, rule in base_rules.items():
            parked = policy["network_policies"][name]
            assert parked["binaries"] == [{"path": PARKED + b["path"]} for b in rule["binaries"]]
            assert {k: v for k, v in parked.items() if k != "binaries"} == {
                k: v for k, v in rule.items() if k != "binaries"
            }
        assert not set(_all_binaries(policy)) & set(sandbox.AGENT_BINARY_PATHS)
        assert _phase_rules(policy)["agentic_ci_phase_0"]["binaries"] == [{"path": SHIM}]

    def test_agent_phase_restores_parked_binaries(self):
        setup = sandbox.build_phase_policy(POLICY_GET_BASE, [NPM], park_agent=True)
        validate = sandbox.build_phase_policy({"policy": setup}, [GOPROXY], park_agent=True)
        assert validate == sandbox.build_phase_policy(POLICY_GET_BASE, [GOPROXY], park_agent=True)
        assert sandbox.build_phase_policy({"policy": validate}, []) == POLICY_GET_BASE["policy"]

    def test_parking_keeps_other_binaries_and_never_duplicates(self):
        output = _base()
        rules = output["policy"]["network_policies"]
        rules["mixed"] = {
            "name": "mixed",
            "endpoints": [{"host": "example.com", "port": 443, "access": "full"}],
            "binaries": [
                {"path": "/usr/bin/git"},
                {"path": "/usr/local/bin/codex"},
                {"path": PARKED + "/usr/local/bin/codex"},
            ],
        }
        parked = sandbox.build_phase_policy(output, [], park_agent=True)
        assert parked["network_policies"]["mixed"]["binaries"] == [
            {"path": "/usr/bin/git"},
            {"path": PARKED + "/usr/local/bin/codex"},
        ]
        restored = sandbox.build_phase_policy({"policy": parked}, [])
        assert restored["network_policies"]["mixed"]["binaries"] == [
            {"path": "/usr/bin/git"},
            {"path": "/usr/local/bin/codex"},
        ]

    def test_parking_preserves_credential_bindings(self):
        output = _base()
        output["policy"]["network_policies"]["allow_oauth2_googleapis_com_443"] = {
            "name": "allow_oauth2_googleapis_com_443",
            "endpoints": [{"host": "oauth2.googleapis.com", "port": 443, "access": "read-write"}],
            "binaries": [{"path": "/usr/local/bin/claude"}],
        }
        bound = build_credential_binding_patch(output)
        setup = sandbox.build_phase_policy({"policy": bound}, [NPM], park_agent=True)
        rule = setup["network_policies"]["allow_oauth2_googleapis_com_443"]
        assert (
            rule["endpoints"]
            == bound["network_policies"]["allow_oauth2_googleapis_com_443"]["endpoints"]
        )
        assert rule["binaries"] == [{"path": PARKED + "/usr/local/bin/claude"}]
        assert sandbox.build_phase_policy({"policy": setup}, []) == bound

    def test_parked_prefix_can_never_be_an_executable(self):
        assert PARKED.startswith("/proc/")
        assert all(not p.startswith(PARKED) for p in sandbox.AGENT_BINARY_PATHS)

    def test_provider_rules_are_not_sent_back(self):
        output = _base()
        output["policy"]["network_policies"]["_provider_ci_openai"] = {
            "name": "_provider_ci_openai",
            "endpoints": [{"host": "api.openai.com", "port": 443}],
            "binaries": [{"path": "/usr/bin/curl"}],
        }
        for park in (False, True):
            policy = sandbox.build_phase_policy(output, [NPM], park_agent=park)
            assert not any(n.startswith("_provider_") for n in policy["network_policies"])

    def test_order_is_deterministic_and_endpoints_deduplicated(self):
        first = sandbox.build_phase_policy(POLICY_GET_BASE, [GOPROXY, NPM, GOPROXY])
        second = sandbox.build_phase_policy(POLICY_GET_BASE, [GOPROXY, NPM])
        assert first == second
        assert list(first["network_policies"]) == list(second["network_policies"])
        assert [r["endpoints"][0]["host"] for r in _phase_rules(first).values()] == [
            "proxy.golang.org",
            "registry.npmjs.org",
        ]
        assert yaml.safe_dump(first) == yaml.safe_dump(second)

    def test_endpoint_options(self):
        policy = sandbox.build_phase_policy(
            POLICY_GET_BASE,
            [
                "api.example.com:8443:read-write:rest:enforce:"
                "allowed-ip=10.0.0.0/8,allowed-ip=10.0.0.0/8,allowed-ip=192.168.1.1",
                "*.example.com:443:full:websocket",
            ],
        )
        endpoints = [r["endpoints"][0] for r in _phase_rules(policy).values()]
        assert endpoints == [
            {
                "host": "api.example.com",
                "port": 8443,
                "access": "read-write",
                "protocol": "rest",
                "enforcement": "enforce",
                "allowed_ips": ["10.0.0.0/8", "192.168.1.1"],
            },
            {"host": "*.example.com", "port": 443, "access": "full", "protocol": "websocket"},
        ]

    @pytest.mark.parametrize(
        "endpoint",
        [
            "api.example.com:443:read-write:::allow-uninspected-credentials",
            "api.example.com:443:read-write:rest::request-body-credential-rewrite",
            "api.example.com:443:full:websocket::allowed-ip=10.0.0.1,websocket-credential-rewrite",
        ],
    )
    def test_credential_options_are_never_given_to_the_shim(self, endpoint):
        with pytest.raises(ValueError, match="phase endpoint 0 has a credential option") as exc:
            sandbox.build_phase_policy(POLICY_GET_BASE, [endpoint])
        assert "api.example.com" not in str(exc.value)

    def test_endpoint_grammar_matches_the_profile_validator(self):
        cases = [
            "registry.npmjs.org:443:read-only",
            "registry.npmjs.org:443:read-only:::",
            "registry.npmjs.org:443:read-only:::allowed-ip=10.0.0.1",
            "registry.npmjs.org:443:read-only:::allowed-ip=example",
            "registry.npmjs.org:443:read-only:rest",
            "registry.npmjs.org:443:read-only::audit",
            "registry.npmjs.org:65536:read-only",
            "*.example.com:443:full",
            "bad host:443:full",
        ]
        for case in cases:
            try:
                sandbox.build_phase_policy(POLICY_GET_BASE, [case])
                accepted = True
            except ValueError:
                accepted = False
            assert accepted == is_raw_endpoint(case), case

    @pytest.mark.parametrize(
        "endpoint",
        [
            "registry.npmjs.org",
            "registry.npmjs.org:443",
            "registry.npmjs.org:0:read-only",
            "registry.npmjs.org:https:read-only",
            "registry.npmjs.org:\u0664\u0664\u0663:read-only",
            "registry.npmjs.org:443:write",
            "registry.npmjs.org:443:read-only:tcp",
            "registry.npmjs.org:443:read-only::enforce",
            "registry.npmjs.org:443:read-only:rest:strict",
            "registry.npmjs.org:443:read-only:::bogus-option",
            "registry.npmjs.org:443:read-only:::allowed-ip=",
            "registry.npmjs.org:443:read-only:::allowed-ip=not-an-ip",
            "registry.npmjs.org:443:read-only:::",
            "-rf:443:read-only",
            "registry.npmjs.org :443:read-only",
            ":443:read-only",
            "a:443:read-only:rest:enforce:allow-uninspected-credentials:extra",
        ],
    )
    def test_rejects_malformed_endpoints_without_echoing_them(self, endpoint):
        with pytest.raises(ValueError, match="phase endpoint 1 ") as excinfo:
            sandbox.build_phase_policy(POLICY_GET_BASE, [NPM, endpoint])
        assert endpoint not in str(excinfo.value)

    @pytest.mark.parametrize(
        ("output", "missing"),
        [
            ({}, "'policy' object"),
            ({"policy": None}, "'policy' object"),
            ([], "'policy' object"),
            ({"policy": {"version": 1, "filesystem_policy": {"secret": "x"}}}, "network_policies"),
            ({"policy": {"network_policies": []}}, "network_policies"),
        ],
    )
    def test_missing_policy_or_rules_raise_without_content(self, output, missing):
        with pytest.raises(ValueError, match=missing) as excinfo:
            sandbox.build_phase_policy(output, [NPM])
        assert "secret" not in str(excinfo.value)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeOpenShell:
    """Stands in for ``sandbox._run``: answers ``policy get`` and records ``policy set``."""

    def __init__(self, get=None, set_rc=0, set_stderr=""):
        self.get = get if get is not None else _completed(stdout=json.dumps(POLICY_GET_BASE))
        self.set_rc = set_rc
        self.set_stderr = set_stderr
        self.calls = []
        self.applied = None
        self.policy_file = None

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["openshell", "policy", "get"]:
            return self.get
        if args[:3] == ["openshell", "policy", "set"]:
            self.policy_file = args[args.index("--policy") + 1]
            with open(self.policy_file) as fh:
                self.applied = yaml.safe_load(fh)
            return _completed(self.set_rc, stderr=self.set_stderr)
        raise AssertionError(f"unexpected command {args}")


class TestApplyPhasePolicy:
    def test_gets_the_base_policy_and_sets_the_whole_object(self):
        fake = _FakeOpenShell()
        with mock.patch.object(sandbox, "_run", fake):
            sandbox.apply_phase_policy("setup", [NPM])

        assert fake.calls[0] == [
            "openshell",
            "policy",
            "get",
            "--base",
            "-o",
            "json",
            sandbox.SANDBOX_NAME,
        ]
        assert fake.calls[1] == [
            "openshell",
            "policy",
            "set",
            "--wait",
            "--policy",
            fake.policy_file,
            sandbox.SANDBOX_NAME,
        ]
        assert len(fake.calls) == 2
        assert fake.applied == sandbox.build_phase_policy(POLICY_GET_BASE, [NPM], park_agent=True)
        assert not os.path.exists(fake.policy_file)

    def test_never_uses_policy_update(self):
        fake = _FakeOpenShell()
        with mock.patch.object(sandbox, "_run", fake):
            sandbox.apply_phase_policy("validate", [NPM])
        assert not any("update" in call for call in fake.calls)

    def test_agent_phase_strips_shim_rules(self):
        setup = sandbox.build_phase_policy(POLICY_GET_BASE, [NPM], park_agent=True)
        fake = _FakeOpenShell(get=_completed(stdout=json.dumps({"policy": setup})))
        with mock.patch.object(sandbox, "_run", fake):
            sandbox.apply_phase_policy("agent", [])
        assert fake.applied == POLICY_GET_BASE["policy"]

    def test_unchanged_policy_is_not_set(self):
        fake = _FakeOpenShell()
        with mock.patch.object(sandbox, "_run", fake):
            sandbox.apply_phase_policy("agent", [])
        assert len(fake.calls) == 1

    @pytest.mark.parametrize("phase", ["setup", "validate"])
    def test_repeating_a_shim_phase_is_not_set(self, phase):
        current = sandbox.build_phase_policy(POLICY_GET_BASE, [NPM], park_agent=True)
        fake = _FakeOpenShell(get=_completed(stdout=json.dumps({"policy": current})))
        with mock.patch.object(sandbox, "_run", fake):
            sandbox.apply_phase_policy(phase, [NPM])
        assert len(fake.calls) == 1

    def test_logs_the_number_of_distinct_endpoints(self, capsys):
        fake = _FakeOpenShell()
        with mock.patch.object(sandbox, "_run", fake):
            sandbox.apply_phase_policy("setup", [NPM, GOPROXY, NPM])
        assert "2 setup-shim endpoint(s) open" in capsys.readouterr().out

    def test_agent_phase_takes_no_endpoints(self):
        with (
            mock.patch.object(sandbox, "_run") as run,
            pytest.raises(ValueError, match="agent phase"),
        ):
            sandbox.apply_phase_policy("agent", [NPM])
        run.assert_not_called()

    def test_unknown_phase(self):
        with (
            mock.patch.object(sandbox, "_run") as run,
            pytest.raises(ValueError, match="phase must be one of"),
        ):
            sandbox.apply_phase_policy("install", [])
        run.assert_not_called()

    def test_set_failure_raises_without_stderr_and_removes_the_file(self, capsys):
        stderr = "filesystem policy cannot be removed; token=sk-live-secret"
        fake = _FakeOpenShell(set_rc=1, set_stderr=stderr)
        with (
            mock.patch.object(sandbox, "_run", fake),
            pytest.raises(RuntimeError) as excinfo,
        ):
            sandbox.apply_phase_policy("setup", [NPM])
        message = str(excinfo.value)
        assert "policy set" in message and "status 1" in message
        assert "sk-live-secret" not in message and "filesystem" not in message
        assert not os.path.exists(fake.policy_file)
        assert "sk-live-secret" in capsys.readouterr().out  # logged for the job log

    def test_temp_file_is_removed_when_the_command_raises(self):
        seen = {}

        def run(args, **kwargs):
            if args[2] == "get":
                return _completed(stdout=json.dumps(POLICY_GET_BASE))
            seen["file"] = args[args.index("--policy") + 1]
            raise OSError("openshell vanished")

        with mock.patch.object(sandbox, "_run", run), pytest.raises(OSError):
            sandbox.apply_phase_policy("setup", [NPM])
        assert seen["file"] and not os.path.exists(seen["file"])

    def test_get_failure_raises_without_stderr(self, capsys):
        fake = _FakeOpenShell(get=_completed(2, stderr="permission denied: secret-detail"))
        with (
            mock.patch.object(sandbox, "_run", fake),
            pytest.raises(RuntimeError) as excinfo,
        ):
            sandbox.apply_phase_policy("setup", [NPM])
        assert "policy get" in str(excinfo.value) and "status 2" in str(excinfo.value)
        assert "secret-detail" not in str(excinfo.value)
        assert len(fake.calls) == 1
        assert "secret-detail" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "stdout", ["not json {secret}", json.dumps({"scope": "sandbox"}), json.dumps([1])]
    )
    def test_unusable_get_output_names_only_the_class(self, stdout):
        fake = _FakeOpenShell(get=_completed(stdout=stdout))
        with (
            mock.patch.object(sandbox, "_run", fake),
            pytest.raises(RuntimeError) as excinfo,
        ):
            sandbox.apply_phase_policy("setup", [NPM])
        message = str(excinfo.value)
        assert "Error)" in message
        assert "secret" not in message and "scope" not in message
        assert len(fake.calls) == 1

    def test_credential_bindings_survive_a_phase_round_trip(self):
        output = _base()
        output["policy"]["network_policies"]["gcp"] = {
            "name": "gcp",
            "endpoints": [{"host": "aiplatform.googleapis.com", "port": 443}],
            "binaries": [{"path": "/usr/local/bin/claude"}],
        }
        bound = build_credential_binding_patch(output)
        state = {"policy": bound}

        def run(args, **kwargs):
            if args[2] == "get":
                return _completed(stdout=json.dumps(state))
            with open(args[args.index("--policy") + 1]) as fh:
                state["policy"] = yaml.safe_load(fh)
            return _completed()

        with mock.patch.object(sandbox, "_run", run):
            sandbox.apply_phase_policy("setup", [NPM])
            sandbox.apply_phase_policy("agent", [])
        assert state["policy"] == bound


class TestCreatePassesProfileToPolicy:
    def _apply(self, **kwargs):
        with (
            mock.patch.object(sandbox, "resolve_endpoints", return_value=["a:443:full"]) as resolve,
            mock.patch.object(sandbox, "_apply_credential_bindings"),
            mock.patch.object(sandbox, "_run") as run,
        ):
            sandbox.create(image="img", auth_mode="openai", workdir="/w", **kwargs)
        return resolve, run

    def test_profile_reaches_resolve_endpoints(self):
        profile = SandboxProfile(egress=("npm",))
        resolve, _ = self._apply(profile=profile)
        assert resolve.call_args.kwargs["profile"] is profile

    def test_argv_without_a_profile_is_unchanged(self, tmp_path, capsys):
        """No profile: the exact ``openshell`` argv origin/main produced before profiles."""
        with (
            mock.patch.object(sandbox, "_apply_credential_bindings") as bind,
            mock.patch.object(sandbox, "_run") as run,
        ):
            sandbox.create(image="img", auth_mode="openai", workdir=str(tmp_path), otel_port=4318)
        binaries = []
        for path in (
            "/usr/local/bin/claude",
            "/usr/local/sbin/claude",
            "/usr/local/bin/opencode",
            "/usr/local/sbin/opencode",
            "/usr/local/bin/codex",
            "/usr/local/sbin/codex",
        ):
            binaries += ["--binary", path]
        endpoints = []
        for endpoint in (
            "github.com:443:full",
            "*.github.com:443:full",
            "gitlab.com:443:full",
            "*.gitlab.com:443:full",
            "pypi.org:443:read-only",
            "files.pythonhosted.org:443:read-only",
            "api.openai.com:443:read-write:::allow-uninspected-credentials",
            "chatgpt.com:443:read-write",
            "host.openshell.internal:4318:read-write",
        ):
            endpoints += ["--add-endpoint", endpoint]
        assert [c.args[0] for c in run.call_args_list] == [
            [
                "openshell",
                "sandbox",
                "create",
                "--name",
                "ci",
                "--no-tty",
                "--no-auto-providers",
                "--provider",
                PROVIDER_NAME,
                "--from",
                "img",
                "--detach",
                "--",
                "sleep",
                "infinity",
            ],
            ["openshell", "policy", "update", "--wait", *binaries, *endpoints, "ci"],
        ]
        bind.assert_called_once_with()
        assert "Policy source: built-in default" in capsys.readouterr().out

    def test_agent_presets_reach_policy_update(self, tmp_path):
        profile = parse_profile({"egress": ["npm"]}, source="central").profile
        with (
            mock.patch.object(sandbox, "_apply_credential_bindings"),
            mock.patch.object(sandbox, "_run") as run,
        ):
            sandbox.create(workdir=str(tmp_path), auth_mode="openai", profile=profile)
        update = run.call_args_list[-1].args[0]
        endpoints = [update[i + 1] for i, a in enumerate(update) if a == "--add-endpoint"]
        assert NPM in endpoints
        binaries = [update[i + 1] for i, a in enumerate(update) if a == "--binary"]
        assert binaries == list(sandbox.AGENT_BINARY_PATHS)
        assert SHIM not in update

    def test_pypi_preset_replaces_the_l4_default_in_policy_update(self, tmp_path):
        profile = parse_profile({"egress": ["pypi"]}, source="central").profile
        with (
            mock.patch.object(sandbox, "_apply_credential_bindings"),
            mock.patch.object(sandbox, "_run") as run,
        ):
            sandbox.create(workdir=str(tmp_path), auth_mode="openai", profile=profile)
        update = run.call_args_list[-1].args[0]
        endpoints = [update[i + 1] for i, a in enumerate(update) if a == "--add-endpoint"]
        pypi = [
            ep for ep in endpoints if ep.split(":")[0] in {"pypi.org", "files.pythonhosted.org"}
        ]
        assert pypi == [
            "pypi.org:443:read-only:rest:enforce",
            "files.pythonhosted.org:443:read-only:rest:enforce",
        ]


class TestSetEgressPhase:
    def _backend(self, profile):
        return create_backend("openshell", harness=mock.Mock(), sandbox_profile=profile)

    @pytest.mark.parametrize("phase", ["setup", "validate", "agent"])
    def test_no_op_without_a_profile(self, phase):
        backend = create_backend("openshell", harness=mock.Mock())
        with mock.patch.object(sandbox, "apply_phase_policy") as apply:
            backend._set_egress_phase(phase)
        apply.assert_not_called()

    @pytest.mark.parametrize("phase", ["setup", "validate"])
    def test_shim_phases_open_the_profile_endpoints(self, phase):
        profile = parse_profile(
            {"egress": ["npm", "internal.example.com:443:read-only"]}, source="central"
        ).profile
        backend = self._backend(profile)
        with mock.patch.object(sandbox, "apply_phase_policy") as apply:
            backend._set_egress_phase(phase)
        apply.assert_called_once_with(phase, [NPM, "internal.example.com:443:read-only"])

    def test_agent_phase_strips_only(self):
        backend = self._backend(SandboxProfile(egress=("npm",)))
        with mock.patch.object(sandbox, "apply_phase_policy") as apply:
            backend._set_egress_phase("agent")
        apply.assert_called_once_with("agent", [])


class TestSandboxIdentityProfileHash:
    def test_identity_without_a_profile_is_unchanged(self):
        assert openshell_backend._sandbox_identity("Codex", "img", "openai") == {
            "auth_mode": "openai",
            "harness": "Codex",
            "image": "img",
        }
        assert openshell_backend._sandbox_identity(
            "Codex", "img", "openai", None
        ) == openshell_backend._sandbox_identity("Codex", "img", "openai")

    def test_identity_includes_the_profile_hash(self):
        profile = SandboxProfile(egress=("npm",))
        identity = openshell_backend._sandbox_identity("Codex", "img", "openai", profile)
        assert identity == {
            "auth_mode": "openai",
            "harness": "Codex",
            "image": "img",
            "profile_hash": profile_hash(profile),
        }

    def _setup(self, backend, tmp_path, monkeypatch, *, exists, saved):
        state = tmp_path / "state.json"
        monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state))
        if saved is not None:
            state.write_text(json.dumps(saved, sort_keys=True) + "\n")
        backend.workdir = str(tmp_path)
        backend.harness.auth_mode_for_env.return_value = "openai"
        backend.harness.name = "Codex"
        openshell = "agentic_ci.backends.openshell"
        with (
            mock.patch(f"{openshell}.provider") as provider,
            mock.patch(f"{openshell}.gateway.is_running", return_value=True),
            mock.patch(f"{openshell}.sandbox.exists", return_value=exists),
            mock.patch(f"{openshell}.sandbox.create") as create,
            mock.patch(f"{openshell}.sandbox.delete") as delete,
            mock.patch(f"{openshell}.sandbox.upload"),
            mock.patch(f"{openshell}.sandbox.apply_phase_policy") as apply_phase,
            mock.patch.object(backend, "_run_setup_steps"),
            mock.patch.object(backend, "_upload_sandbox_config"),
        ):
            provider.provider_exists.return_value = exists
            provider.auth_mode.return_value = "openai"
            provider.requires_provider.return_value = True
            backend.setup()
        self.apply_phase = apply_phase
        return create, delete, state

    def test_saved_identity_file_without_a_profile_is_unchanged(self, tmp_path, monkeypatch):
        backend = create_backend("openshell", harness=mock.Mock())
        create, _, state = self._setup(backend, tmp_path, monkeypatch, exists=False, saved=None)
        assert "profile" not in create.call_args.kwargs
        assert state.read_text() == '{"auth_mode": "openai", "harness": "Codex", "image": null}\n'

    def test_create_gets_the_profile_and_the_identity_records_it(self, tmp_path, monkeypatch):
        profile = SandboxProfile(egress=("npm",))
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=profile)
        create, _, state = self._setup(backend, tmp_path, monkeypatch, exists=False, saved=None)
        assert create.call_args.kwargs["profile"] is profile
        assert json.loads(state.read_text())["profile_hash"] == profile_hash(profile)

    def test_a_changed_profile_recreates_the_sandbox(self, tmp_path, monkeypatch):
        old = SandboxProfile(egress=("npm",))
        new = SandboxProfile(egress=("npm", "goproxy"))
        saved = openshell_backend._sandbox_identity("Codex", None, "openai", old)
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=new)
        create, delete, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        delete.assert_called_once_with()
        create.assert_called_once()

    def test_the_same_profile_reuses_the_sandbox(self, tmp_path, monkeypatch):
        profile = SandboxProfile(egress=("npm",))
        saved = openshell_backend._sandbox_identity("Codex", None, "openai", profile)
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=profile)
        create, delete, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        delete.assert_not_called()
        create.assert_not_called()
        # A run that stopped in the setup or validate phase must not leave the
        # shim's rules live (or the agent's parked) for the next agent.
        self.apply_phase.assert_called_once_with("agent", [])

    def test_reuse_without_a_profile_does_not_touch_the_policy(self, tmp_path, monkeypatch):
        saved = openshell_backend._sandbox_identity("Codex", None, "openai")
        backend = create_backend("openshell", harness=mock.Mock())
        create, delete, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        delete.assert_not_called()
        create.assert_not_called()
        self.apply_phase.assert_not_called()

    def test_a_new_sandbox_needs_no_phase_switch(self, tmp_path, monkeypatch):
        backend = create_backend(
            "openshell", harness=mock.Mock(), sandbox_profile=SandboxProfile(egress=("npm",))
        )
        self._setup(backend, tmp_path, monkeypatch, exists=False, saved=None)
        self.apply_phase.assert_not_called()

    def test_adding_a_profile_recreates_a_sandbox_made_without_one(self, tmp_path, monkeypatch):
        saved = openshell_backend._sandbox_identity("Codex", None, "openai")
        backend = create_backend(
            "openshell", harness=mock.Mock(), sandbox_profile=SandboxProfile(egress=("npm",))
        )
        create, delete, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        delete.assert_called_once_with()
        create.assert_called_once()
