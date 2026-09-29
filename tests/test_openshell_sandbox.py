"""Tests for OpenShell sandbox lifecycle commands."""

import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
import yaml

import agentic_ci.backends.openshell as openshell_backend
from agentic_ci import toolchains
from agentic_ci.backends import create_backend
from agentic_ci.backends.openshell import provider as openshell_provider
from agentic_ci.backends.openshell import sandbox
from agentic_ci.backends.openshell.policy import (
    BASE_POLICY,
    EGRESS_PRESETS,
    phase_endpoints,
)
from agentic_ci.backends.openshell.provider import PROVIDER_NAME
from agentic_ci.backends.openshell.provision import OpenShellInstaller
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
# The main process of the September 2026 nested-OpenShell probe sandbox.
MAIN = sandbox.MainProcess(pid=19, start_time=214392511)
_PHASE_STEPS = (
    "find_main_process",
    "stop_leftover_processes",
    "provider_env_state",
    "detach_provider",
    "apply_phase_policy",
    "attach_provider",
    "wait_for_provider_env",
)


@contextlib.contextmanager
def _phase_steps():
    """Patch what a profile setup or phase switch runs in the sandbox.

    One Mock stands in for every step, so ``steps.mock_calls`` is their order.
    """
    steps = mock.Mock()
    steps.find_main_process.return_value = MAIN
    steps.provider_env_state.return_value = "ATTACHED"
    with contextlib.ExitStack() as stack:
        for name in _PHASE_STEPS:
            stack.enter_context(mock.patch.object(sandbox, name, getattr(steps, name)))
        yield steps


def _with_credential_binding(output):
    """The ``.policy`` of *output* with a credential binding on each GCP endpoint.

    Bindings like these come from sandbox policies written for OpenShell's
    endpointless google-cloud provider. agentic-ci no longer writes them, but
    a phase switch must still carry any endpoint field through unchanged.
    """
    policy = copy.deepcopy(output["policy"])
    for rule in policy["network_policies"].values():
        for endpoint in rule.get("endpoints", []):
            if endpoint.get("host", "").endswith("googleapis.com"):
                endpoint["credential_binding"] = {"provider": PROVIDER_NAME}
                endpoint["allow_uninspected_credentials"] = True
    return policy


def _without_policy_flag(args):
    """Drop the ``--policy <tempfile>`` pair, whose path differs per call."""
    index = args.index("--policy")
    return args[:index] + args[index + 2 :]


@pytest.fixture(autouse=True)
def gateway_config_current():
    """Treat the on-disk gateway config as matching the environment by default."""
    with mock.patch("agentic_ci.backends.openshell.gateway.config_is_current", return_value=True):
        yield


@pytest.fixture(autouse=True)
def provider_profile_current():
    """Treat the gateway's copy of a reused provider's profile as current by default."""
    with mock.patch(
        "agentic_ci.backends.openshell.provider.ensure_auth_mode_profile", return_value=False
    ):
        yield


def _profile_identity(harness, image, auth_mode, profile, main=MAIN):
    """A saved identity for a profile sandbox, with its main process recorded."""
    identity = openshell_backend._sandbox_identity(harness, image, auth_mode, profile=profile)
    if main is not None:
        identity["main_process"] = main.to_record()
    return identity


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
        assert _without_policy_flag(created()) == [
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


@pytest.mark.parametrize("auth_mode", [None, "vertex", "openai", "api-key", "oauth"])
def test_apply_policy_only_updates_the_policy(auth_mode):
    """No credential binding is patched in afterwards, for any auth mode.

    The provider's credentials resolve through the rule OpenShell composes
    from its profile, and OpenShell refuses a sandbox policy that binds a
    provider whose profile declares endpoints.
    """
    with (
        mock.patch.object(sandbox, "resolve_endpoints", return_value=["github.com:443:full"]),
        mock.patch.object(sandbox, "_run") as run,
    ):
        sandbox._apply_policy(policy_path=None, auth_mode=auth_mode)

    assert [c.args[0][:3] for c in run.call_args_list] == [["openshell", "policy", "update"]]


class TestCreateBasePolicy:
    def test_passes_the_base_policy_file_and_removes_it(self):
        seen = {}

        def capture(args, **kwargs):
            path = args[args.index("--policy") + 1]
            with open(path) as fh:
                seen["text"] = fh.read()
            seen["path"] = path

        with (
            mock.patch.object(sandbox, "_run", side_effect=capture),
            mock.patch.object(sandbox, "_apply_policy"),
        ):
            sandbox.create()

        assert yaml.safe_load(seen["text"]) == BASE_POLICY
        # The v0.1.x policy schema refuses null fields.
        assert not re.search(r":\s*(null|~)\s*$", seen["text"], re.MULTILINE)
        assert not os.path.exists(seen["path"])

    def test_policy_flag_precedes_the_command_terminator(self, created):
        args = created()
        assert args.index("--policy") < args.index("--")

    def test_policy_file_removed_when_create_fails(self):
        seen = {}

        def fail(args, **kwargs):
            seen["path"] = args[args.index("--policy") + 1]
            raise subprocess.CalledProcessError(1, args)

        with (
            mock.patch.object(sandbox, "_run", side_effect=fail),
            pytest.raises(subprocess.CalledProcessError),
        ):
            sandbox.create()

        assert not os.path.exists(seen["path"])


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
        identity = _profile_identity(
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
            _phase_steps(),
            mock.patch(f"{openshell}.log.info") as logged,
            mock.patch.object(backend, "_run_setup_steps"),
            mock.patch.object(backend, "_upload_sandbox_config"),
        ):
            provider.provider_exists.return_value = exists
            provider.auth_mode.return_value = "vertex"
            # Vertex credentials also reach the sandbox outside the provider,
            # so they have no fingerprint in the sandbox identity.
            provider.credential_fingerprint.return_value = None
            provider.ensure_auth_mode_profile.return_value = False
            provider.provider_env_vars.side_effect = openshell_provider.provider_env_vars
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
        bound = {"policy": _with_credential_binding(output)}
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
        bound = _with_credential_binding(output)
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
    @pytest.mark.parametrize("command", ["get", "set"])
    def test_gateway_timeout_fails_closed_and_cleans_up(self, command):
        fake = _FakeOpenShell()
        kwargs_seen = {}

        def run(args, **kwargs):
            if args[:3] == ["openshell", "policy", command]:
                kwargs_seen.update(kwargs)
                if command == "set":
                    fake.policy_file = args[args.index("--policy") + 1]
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return fake(args, **kwargs)

        with mock.patch.object(sandbox, "_run", run):
            with pytest.raises(RuntimeError, match=f"policy {command} timed out") as err:
                sandbox.apply_phase_policy("setup", [NPM])

        assert kwargs_seen["timeout"] > 0
        assert "openshell" not in str(err.value).split(":")[0]
        if command == "set":
            assert not os.path.exists(fake.policy_file)

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
        bound = _with_credential_binding(output)
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
            mock.patch.object(sandbox, "_run") as run,
        ):
            sandbox.create(image="img", auth_mode="openai", workdir="/w", **kwargs)
        return resolve, run

    def test_profile_reaches_resolve_endpoints(self):
        profile = SandboxProfile(egress=("npm",))
        resolve, _ = self._apply(profile=profile)
        assert resolve.call_args.kwargs["profile"] is profile

    def test_argv_without_a_profile_is_unchanged(self, tmp_path, capsys):
        """No profile: the exact ``openshell`` argv before profiles, plus the base policy."""
        with mock.patch.object(sandbox, "_run") as run:
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
        calls = [c.args[0] for c in run.call_args_list]
        assert [_without_policy_flag(calls[0]), *calls[1:]] == [
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
        assert "Policy source: built-in default" in capsys.readouterr().out

    def test_agent_presets_reach_policy_update(self, tmp_path):
        profile = parse_profile({"egress": ["npm"]}, source="central").profile
        with (
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
    PROFILE = SandboxProfile(egress=("npm",))

    def _backend(self, profile, auth_mode="openai", main=MAIN):
        harness = mock.Mock()
        harness.auth_mode_for_env.return_value = auth_mode
        backend = create_backend("openshell", harness=harness, sandbox_profile=profile)
        backend._main_process = main
        return backend

    @pytest.mark.parametrize("phase", ["setup", "validate", "agent"])
    def test_no_op_without_a_profile(self, phase):
        backend = self._backend(None, main=None)
        with _phase_steps() as steps:
            backend._set_egress_phase(phase)
        # No kill, no detach or attach, no policy change.
        assert steps.mock_calls == []

    @pytest.mark.parametrize("phase", ["setup", "validate"])
    def test_shim_phases_open_the_profile_endpoints(self, phase):
        profile = parse_profile(
            {"egress": ["npm", "internal.example.com:443:read-only"]}, source="central"
        ).profile
        backend = self._backend(profile)
        with _phase_steps() as steps:
            backend._set_egress_phase(phase)
        steps.apply_phase_policy.assert_called_once_with(
            phase, [NPM, "internal.example.com:443:read-only"]
        )

    @pytest.mark.parametrize(
        ("auth_mode", "env_vars"),
        [
            ("openai", ("OPENAI_API_KEY",)),
            ("api-key", ("ANTHROPIC_API_KEY",)),
            ("vertex", ("GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN", "GOOGLE_VERTEX_AI_TOKEN")),
        ],
    )
    @pytest.mark.parametrize("phase", ["setup", "validate"])
    def test_shim_phase_kills_leftovers_then_detaches_before_the_policy(
        self, phase, auth_mode, env_vars
    ):
        # Vertex too: its profile declares the aiplatform hosts, so OpenShell
        # composes a rule for the agent binaries that parking cannot touch.
        backend = self._backend(self.PROFILE, auth_mode)
        with _phase_steps() as steps:
            backend._set_egress_phase(phase)
        assert steps.mock_calls == [
            mock.call.stop_leftover_processes(MAIN, phase),
            mock.call.provider_env_state(env_vars),
            mock.call.detach_provider(phase),
            mock.call.apply_phase_policy(phase, [NPM]),
            mock.call.wait_for_provider_env(env_vars, attached=False, phase=phase),
        ]

    @pytest.mark.parametrize("seen", ["DETACHED", None])
    @pytest.mark.parametrize("phase", ["setup", "validate"])
    def test_a_provider_not_seen_attached_is_attached_before_the_detach(self, phase, seen):
        # A DETACHED probe with no ATTACHED before it proves nothing (setup
        # straight to validate, or a probe that cannot see the placeholder).
        backend = self._backend(self.PROFILE)
        with _phase_steps() as steps:
            steps.provider_env_state.return_value = seen
            backend._set_egress_phase(phase)
        assert steps.mock_calls == [
            mock.call.stop_leftover_processes(MAIN, phase),
            mock.call.provider_env_state(("OPENAI_API_KEY",)),
            mock.call.attach_provider(phase),
            mock.call.wait_for_provider_env(("OPENAI_API_KEY",), attached=True, phase=phase),
            mock.call.detach_provider(phase),
            mock.call.apply_phase_policy(phase, [NPM]),
            mock.call.wait_for_provider_env(("OPENAI_API_KEY",), attached=False, phase=phase),
        ]

    def test_a_provider_never_seen_attached_fails_closed(self):
        backend = self._backend(self.PROFILE)
        with _phase_steps() as steps:
            steps.provider_env_state.return_value = "DETACHED"
            steps.wait_for_provider_env.side_effect = RuntimeError("not confirmed")
            with pytest.raises(RuntimeError, match="not confirmed"):
                backend._set_egress_phase("setup")
        steps.detach_provider.assert_not_called()
        steps.apply_phase_policy.assert_not_called()

    @pytest.mark.parametrize(
        ("auth_mode", "env_vars"),
        [
            ("openai", ("OPENAI_API_KEY",)),
            ("api-key", ("ANTHROPIC_API_KEY",)),
            ("vertex", ("GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN", "GOOGLE_VERTEX_AI_TOKEN")),
        ],
    )
    def test_agent_phase_restores_the_policy_then_reattaches(self, auth_mode, env_vars):
        backend = self._backend(self.PROFILE, auth_mode)
        with _phase_steps() as steps:
            backend._set_egress_phase("agent")
        assert steps.mock_calls == [
            mock.call.stop_leftover_processes(MAIN, "agent"),
            mock.call.apply_phase_policy("agent", []),
            mock.call.attach_provider("agent"),
            mock.call.wait_for_provider_env(env_vars, attached=True, phase="agent"),
        ]

    @pytest.mark.parametrize("phase", ["setup", "validate", "agent"])
    def test_no_provider_means_no_detach(self, phase):
        auth_mode = "oauth"
        backend = self._backend(self.PROFILE, auth_mode)
        with _phase_steps() as steps:
            backend._set_egress_phase(phase)
        assert steps.mock_calls == [
            mock.call.stop_leftover_processes(MAIN, phase),
            mock.call.apply_phase_policy(phase, [] if phase == "agent" else [NPM]),
        ]

    @pytest.mark.parametrize("phase", ["setup", "validate", "agent"])
    def test_a_failed_kill_stops_the_switch(self, phase):
        backend = self._backend(self.PROFILE)
        with _phase_steps() as steps:
            steps.stop_leftover_processes.side_effect = RuntimeError("1 still running")
            with pytest.raises(RuntimeError, match="1 still running"):
                backend._set_egress_phase(phase)
        assert steps.mock_calls == [mock.call.stop_leftover_processes(MAIN, phase)]

    def test_a_failed_detach_leaves_the_shim_closed(self):
        backend = self._backend(self.PROFILE)
        with _phase_steps() as steps:
            steps.detach_provider.side_effect = RuntimeError("detach failed")
            with pytest.raises(RuntimeError, match="detach failed"):
                backend._set_egress_phase("setup")
        steps.apply_phase_policy.assert_not_called()

    def test_an_unconfirmed_detach_fails_the_switch(self):
        backend = self._backend(self.PROFILE)
        with _phase_steps() as steps:
            steps.wait_for_provider_env.side_effect = RuntimeError("not confirmed")
            with pytest.raises(RuntimeError, match="not confirmed"):
                backend._set_egress_phase("validate")

    def test_an_unknown_phase_does_nothing(self):
        backend = self._backend(self.PROFILE)
        with _phase_steps() as steps, pytest.raises(ValueError):
            backend._set_egress_phase("install")
        assert steps.mock_calls == []

    def test_the_main_process_comes_from_the_saved_identity(self, tmp_path, monkeypatch):
        state = tmp_path / "state.json"
        monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state))
        other = sandbox.MainProcess(pid=7, start_time=42)
        state.write_text(
            json.dumps(_profile_identity("Codex", None, "openai", self.PROFILE, main=other))
        )
        backend = self._backend(self.PROFILE, main=None)
        with _phase_steps() as steps:
            backend._set_egress_phase("setup")
        steps.stop_leftover_processes.assert_called_once_with(other, "setup")

    @pytest.mark.parametrize(
        "record", [None, {}, {"pid": "19", "start_time": 1}, {"pid": 0, "start_time": 1}]
    )
    def test_no_recorded_main_process_fails_closed(self, tmp_path, monkeypatch, record):
        state = tmp_path / "state.json"
        monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state))
        identity = _profile_identity("Codex", None, "openai", self.PROFILE, main=None)
        if record is not None:
            identity["main_process"] = record
        state.write_text(json.dumps(identity))
        backend = self._backend(self.PROFILE, main=None)
        with _phase_steps() as steps, pytest.raises(RuntimeError, match="main process"):
            backend._set_egress_phase("validate")
        assert steps.mock_calls == []


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
        identity = openshell_backend._sandbox_identity("Codex", "img", "openai", profile=profile)
        assert identity == {
            "auth_mode": "openai",
            "harness": "Codex",
            "image": "img",
            "profile_hash": profile_hash(profile),
        }

    def _setup(self, backend, tmp_path, monkeypatch, *, exists, saved, configure=None):
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
            _phase_steps() as steps,
            mock.patch.object(backend, "_run_setup_steps") as setup_steps,
            mock.patch.object(backend, "_upload_sandbox_config"),
        ):
            steps.attach_mock(setup_steps, "run_setup_steps")
            steps.attach_mock(create, "create")
            provider.provider_exists.return_value = exists
            provider.auth_mode.return_value = "openai"
            provider.requires_provider.return_value = True
            provider.credential_fingerprint.return_value = None
            provider.ensure_auth_mode_profile.return_value = False
            provider.provider_env_vars.side_effect = openshell_provider.provider_env_vars
            if configure is not None:
                configure(steps)
            backend.setup()
        self.apply_phase = steps.apply_phase_policy
        self.steps = steps
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
        saved = _profile_identity("Codex", None, "openai", old)
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=new)
        create, delete, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        delete.assert_called_once_with()
        create.assert_called_once()

    def test_the_same_profile_reuses_the_sandbox(self, tmp_path, monkeypatch):
        profile = SandboxProfile(egress=("npm",))
        saved = _profile_identity("Codex", None, "openai", profile)
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=profile)
        create, delete, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        delete.assert_not_called()
        create.assert_not_called()
        # A run that stopped in the setup or validate phase must not leave the
        # shim's rules live (or the agent's parked), its processes running or
        # the provider detached for the next agent.
        assert self.steps.mock_calls == [
            mock.call.stop_leftover_processes(MAIN, "agent"),
            mock.call.apply_phase_policy("agent", []),
            mock.call.attach_provider("agent"),
            mock.call.wait_for_provider_env(("OPENAI_API_KEY",), attached=True, phase="agent"),
        ]

    def test_a_profile_sandbox_without_a_recorded_main_process_is_recreated(
        self, tmp_path, monkeypatch, capsys
    ):
        profile = SandboxProfile(egress=("npm",))
        saved = _profile_identity("Codex", None, "openai", profile, main=None)
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=profile)
        create, delete, state = self._setup(
            backend, tmp_path, monkeypatch, exists=True, saved=saved
        )
        delete.assert_called_once_with()
        create.assert_called_once()
        assert json.loads(state.read_text())["main_process"] == MAIN.to_record()
        logged = capsys.readouterr().out
        assert "Sandbox has no recorded main process; recreating" in logged
        assert "Sandbox identity changed" not in logged

    @pytest.mark.parametrize(
        "failing",
        [
            "stop_leftover_processes",
            "apply_phase_policy",
            "attach_provider",
            "wait_for_provider_env",
        ],
    )
    def test_a_sandbox_that_cannot_be_prepared_is_recreated(
        self, tmp_path, monkeypatch, capsys, failing
    ):
        # A survivor, a stale main process record or an unconfirmed attach
        # would otherwise fail every later run on the same sandbox.
        profile = SandboxProfile(egress=("npm",))
        saved = _profile_identity("Codex", None, "openai", profile)
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=profile)

        def configure(steps):
            getattr(steps, failing).side_effect = RuntimeError("Could not stop: 1 survived")

        create, delete, state = self._setup(
            backend, tmp_path, monkeypatch, exists=True, saved=saved, configure=configure
        )
        delete.assert_called_once_with()
        create.assert_called_once()
        assert json.loads(state.read_text())["main_process"] == MAIN.to_record()
        logged = capsys.readouterr().out
        assert "could not prepare the existing sandbox: Could not stop: 1 survived" in logged
        assert "Sandbox could not be reused; recreating OpenShell sandbox" in logged

    def test_a_new_profile_sandbox_records_its_main_process_first(self, tmp_path, monkeypatch):
        profile = SandboxProfile(egress=("npm",))
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=profile)
        _, _, state = self._setup(backend, tmp_path, monkeypatch, exists=False, saved=None)
        names = [c[0] for c in self.steps.mock_calls]
        assert names == ["create", "find_main_process", "run_setup_steps"]
        saved = json.loads(state.read_text())
        assert saved["main_process"] == {"pid": MAIN.pid, "start_time": MAIN.start_time}
        # The record is kept out of identity matching.
        assert openshell_backend._identity_fields(saved) == openshell_backend._sandbox_identity(
            "Codex", None, "openai", profile=profile
        )

    def test_no_profile_records_no_main_process(self, tmp_path, monkeypatch):
        backend = create_backend("openshell", harness=mock.Mock())
        self._setup(backend, tmp_path, monkeypatch, exists=False, saved=None)
        self.steps.find_main_process.assert_not_called()

    def test_reuse_without_a_profile_does_not_touch_the_policy(self, tmp_path, monkeypatch):
        saved = openshell_backend._sandbox_identity("Codex", None, "openai")
        backend = create_backend("openshell", harness=mock.Mock())
        create, delete, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        delete.assert_not_called()
        create.assert_not_called()
        assert self.steps.mock_calls == []

    def test_a_new_sandbox_needs_no_phase_switch(self, tmp_path, monkeypatch):
        backend = create_backend(
            "openshell", harness=mock.Mock(), sandbox_profile=SandboxProfile(egress=("npm",))
        )
        self._setup(backend, tmp_path, monkeypatch, exists=False, saved=None)
        self.apply_phase.assert_not_called()
        self.steps.stop_leftover_processes.assert_not_called()
        self.steps.detach_provider.assert_not_called()
        self.steps.attach_provider.assert_not_called()

    def test_adding_a_profile_recreates_a_sandbox_made_without_one(self, tmp_path, monkeypatch):
        saved = openshell_backend._sandbox_identity("Codex", None, "openai")
        backend = create_backend(
            "openshell", harness=mock.Mock(), sandbox_profile=SandboxProfile(egress=("npm",))
        )
        create, delete, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        delete.assert_called_once_with()
        create.assert_called_once()


GO = SandboxProfile(toolchains={"go": "auto", "shfmt": "3.12.0"})
_RESULTS = (
    toolchains.ToolchainResult("go", "auto", "1.26.5", "a" * 64, "installed"),
    toolchains.ToolchainResult("shfmt", "3.12.0", "", "", "failed", "shfmt: checksum mismatch"),
)
_ENV = toolchains.ToolchainEnv(path=("/sandbox/.local/toolchains/go-1.26.5/go/bin",))


class TestToolchainProvisioning:
    """setup() provisions toolchains after the workdir upload, and only with toolchains."""

    def _setup(self, backend, tmp_path, monkeypatch, *, exists=False, saved=None):
        state = tmp_path / "state.json"
        monkeypatch.setenv("AGENTIC_CI_OPENSHELL_STATE", str(state))
        if saved is not None:
            state.write_text(json.dumps(saved, sort_keys=True) + "\n")
        backend.workdir = str(tmp_path / "work")
        backend.harness.auth_mode_for_env.return_value = "openai"
        backend.harness.name = "Codex"
        openshell = "agentic_ci.backends.openshell"
        with (
            mock.patch(f"{openshell}.provider") as provider,
            mock.patch(f"{openshell}.gateway.is_running", return_value=True),
            mock.patch(f"{openshell}.sandbox.exists", return_value=exists),
            mock.patch(f"{openshell}.sandbox.create") as create,
            mock.patch(f"{openshell}.sandbox.delete") as delete,
            mock.patch(f"{openshell}.sandbox.upload") as upload,
            mock.patch(f"{openshell}.toolchains.provision") as provision,
            _phase_steps() as steps,
            mock.patch.object(backend, "_run_setup_steps") as setup_steps,
            mock.patch.object(backend, "_upload_sandbox_config") as upload_config,
        ):
            provision.return_value = toolchains.Provisioned(results=_RESULTS, env=_ENV)
            for name, part in [
                ("create", create),
                ("delete", delete),
                ("run_setup_steps", setup_steps),
                ("upload", upload),
                ("provision", provision),
                ("upload_config", upload_config),
            ]:
                steps.attach_mock(part, name)
            provider.provider_exists.return_value = exists
            provider.auth_mode.return_value = "openai"
            provider.requires_provider.return_value = True
            provider.credential_fingerprint.return_value = None
            provider.ensure_auth_mode_profile.return_value = False
            provider.provider_env_vars.side_effect = openshell_provider.provider_env_vars
            backend.setup()
        return [c[0] for c in steps.mock_calls], provision, state

    def test_order_create_upload_provision_config(self, tmp_path, monkeypatch):
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=GO)
        backend.run_dir = tmp_path / "_run"
        names, provision, _ = self._setup(backend, tmp_path, monkeypatch)
        assert names == [
            "create",
            "find_main_process",
            "run_setup_steps",
            "upload",
            "provision",
            "upload_config",
        ]
        args, kwargs = provision.call_args
        assert args == (GO.toolchains,)
        assert kwargs["workdir"] == tmp_path / "work"
        assert isinstance(kwargs["installer"], OpenShellInstaller)
        assert backend.toolchain_env == _ENV
        assert backend.toolchain_results == _RESULTS
        assert json.loads((tmp_path / "_run" / "toolchains.json").read_text()) == [
            r.to_dict() for r in _RESULTS
        ]

    def test_failures_are_logged_and_setup_continues(self, tmp_path, monkeypatch, capsys):
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=GO)
        self._setup(backend, tmp_path, monkeypatch)
        assert "WARNING: 1 toolchain(s) not provisioned; the run continues" in (
            capsys.readouterr().out
        )

    def test_without_run_dir_nothing_is_written(self, tmp_path, monkeypatch):
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=GO)
        monkeypatch.chdir(tmp_path)
        with mock.patch.object(toolchains, "write_results") as write_results:
            self._setup(backend, tmp_path, monkeypatch)
        write_results.assert_not_called()
        assert list(tmp_path.rglob("toolchains.json")) == []

    def test_a_new_sandbox_records_its_toolchains_on_the_host(self, tmp_path, monkeypatch):
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=GO)
        _, provision, state = self._setup(backend, tmp_path, monkeypatch)
        # Nothing recorded yet: the installer trusts no marker in a new sandbox.
        assert provision.call_args.kwargs["installer"].recorded == {}
        saved = json.loads(state.read_text())
        # Only what was installed; the failed shfmt is not recorded.
        assert saved["toolchains"] == {"/sandbox/.local/toolchains/go-1.26.5": "a" * 64}
        assert openshell_backend._identity_fields(saved) == openshell_backend._sandbox_identity(
            "Codex", None, "openai", profile=GO
        )

    def test_a_reused_sandbox_trusts_only_the_host_record(self, tmp_path, monkeypatch):
        saved = _profile_identity("Codex", None, "openai", GO)
        saved["toolchains"] = {
            "/sandbox/.local/toolchains/go-1.26.4": "b" * 64,
            "/sandbox/.local/toolchains/go-1.26.5": "c" * 64,
            "/elsewhere/go": "d" * 64,
            "/sandbox/.local/toolchains/bad": "not hex",
        }
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=GO)
        names, provision, state = self._setup(
            backend, tmp_path, monkeypatch, exists=True, saved=saved
        )
        assert "create" not in names and "delete" not in names
        assert provision.call_args.kwargs["installer"].recorded == {
            "/sandbox/.local/toolchains/go-1.26.4": "b" * 64,
            "/sandbox/.local/toolchains/go-1.26.5": "c" * 64,
        }
        after = json.loads(state.read_text())
        assert after["toolchains"] == {
            "/sandbox/.local/toolchains/go-1.26.4": "b" * 64,
            "/sandbox/.local/toolchains/go-1.26.5": "a" * 64,
        }
        assert after["main_process"] == saved["main_process"]

    def test_a_failed_install_drops_its_record(self, tmp_path, monkeypatch):
        saved = _profile_identity("Codex", None, "openai", GO)
        saved["toolchains"] = {"/sandbox/.local/toolchains/go-1.26.5": "a" * 64}
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=GO)
        failed = toolchains.ToolchainResult("go", "auto", "1.26.5", "a" * 64, "failed", "x")
        with mock.patch.object(
            toolchains,
            "provision",
            return_value=toolchains.Provisioned(results=(failed,), env=toolchains.ToolchainEnv()),
        ):
            backend._provision_toolchains(openshell_backend._toolchain_records(saved))
        assert backend._toolchain_records == {}

    def test_a_profile_without_toolchains_records_none(self, tmp_path, monkeypatch):
        backend = create_backend(
            "openshell", harness=mock.Mock(), sandbox_profile=SandboxProfile(egress=("npm",))
        )
        _, _, state = self._setup(backend, tmp_path, monkeypatch)
        assert "toolchains" not in json.loads(state.read_text())

    @pytest.mark.parametrize("profile", [None, SandboxProfile(egress=("npm",))])
    def test_no_toolchains_no_provisioning(self, tmp_path, monkeypatch, profile):
        kwargs = {} if profile is None else {"sandbox_profile": profile}
        backend = create_backend("openshell", harness=mock.Mock(), **kwargs)
        backend.run_dir = tmp_path / "_run"
        names, provision, _ = self._setup(backend, tmp_path, monkeypatch)
        provision.assert_not_called()
        assert "provision" not in names
        assert not backend.toolchain_env
        assert not (tmp_path / "_run").exists()

    def test_a_reused_sandbox_provisions_after_the_agent_switch(self, tmp_path, monkeypatch):
        saved = _profile_identity("Codex", None, "openai", GO)
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=GO)
        names, provision, _ = self._setup(backend, tmp_path, monkeypatch, exists=True, saved=saved)
        assert "create" not in names and "upload" not in names
        assert names.index("provision") > names.index("wait_for_provider_env")
        provision.assert_called_once()
        assert backend.toolchain_env == _ENV

    def test_changed_toolchains_recreate_the_sandbox(self, tmp_path, monkeypatch):
        old = SandboxProfile(toolchains={"go": "1.26.4"})
        saved = _profile_identity("Codex", None, "openai", old)
        saved["toolchains"] = {"/sandbox/.local/toolchains/go-1.26.4": "b" * 64}
        backend = create_backend("openshell", harness=mock.Mock(), sandbox_profile=GO)
        names, provision, state = self._setup(
            backend, tmp_path, monkeypatch, exists=True, saved=saved
        )
        assert names[:2] == ["delete", "create"]
        after = json.loads(state.read_text())
        assert after["profile_hash"] == profile_hash(GO)
        # The old sandbox's record does not carry over to the new one.
        assert provision.call_args.kwargs["installer"].recorded == {}
        assert after["toolchains"] == {"/sandbox/.local/toolchains/go-1.26.5": "a" * 64}


def _agent_harness():
    harness = mock.Mock()
    harness.build_args.return_value = ["agent"]
    harness.auth_mode_for_env.return_value = "openai"
    return harness


class TestToolchainResultsAfterRun:
    """run() rewrites _run/toolchains.json after the workdir download."""

    def _run(self, backend, tmp_path, forge):
        def download(sandbox_path, local_dest):
            if forge:
                path = tmp_path / "_run" / "toolchains.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('[{"name": "go", "status": "installed", "reason": "forged"}]')

        with (
            mock.patch.object(backend, "_write_env_script"),
            mock.patch.object(backend, "_process_stream", return_value=(0, True)),
            mock.patch.object(backend, "_wait_for_otel_flush"),
            mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd_streaming"),
            mock.patch("agentic_ci.backends.openshell.sandbox.download", side_effect=download),
        ):
            return backend.run("prompt", "model")

    def test_a_forged_record_from_the_sandbox_is_replaced(self, tmp_path):
        backend = create_backend(
            "openshell", harness=_agent_harness(), workdir=str(tmp_path), sandbox_profile=GO
        )
        backend.run_dir = tmp_path / "_run"
        backend.toolchain_results = _RESULTS
        self._run(backend, tmp_path, forge=True)
        assert json.loads((tmp_path / "_run" / "toolchains.json").read_text()) == [
            r.to_dict() for r in _RESULTS
        ]

    def test_also_when_the_download_fails(self, tmp_path):
        backend = create_backend(
            "openshell", harness=_agent_harness(), workdir=str(tmp_path), sandbox_profile=GO
        )
        backend.run_dir = tmp_path / "_run"
        backend.toolchain_results = _RESULTS
        error = subprocess.CalledProcessError(1, ["openshell"])

        def download(sandbox_path, local_dest):
            (tmp_path / "_run").mkdir(exist_ok=True)
            (tmp_path / "_run" / "toolchains.json").write_text("forged")
            raise error

        with (
            mock.patch.object(backend, "_write_env_script"),
            mock.patch.object(backend, "_process_stream", return_value=(0, True)),
            mock.patch.object(backend, "_wait_for_otel_flush"),
            mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd_streaming"),
            mock.patch("agentic_ci.backends.openshell.sandbox.download", side_effect=download),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                backend.run("prompt", "model")
        assert json.loads((tmp_path / "_run" / "toolchains.json").read_text()) == [
            r.to_dict() for r in _RESULTS
        ]

    def test_a_symlinked_run_dir_is_not_written_through(self, tmp_path, capsys):
        backend = create_backend(
            "openshell", harness=_agent_harness(), workdir=str(tmp_path / "w"), sandbox_profile=GO
        )
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "w").mkdir()
        (tmp_path / "w" / "_run").symlink_to(outside)
        backend.run_dir = tmp_path / "w" / "_run"
        backend.toolchain_results = _RESULTS
        backend._write_toolchain_results()
        assert list(outside.iterdir()) == []
        assert "run directory is a symlink" in capsys.readouterr().out

    def _run_with(self, backend, download, rc=7):
        with (
            mock.patch.object(backend, "_write_env_script"),
            mock.patch.object(backend, "_process_stream", return_value=(rc, False)),
            mock.patch.object(backend, "_wait_for_otel_flush"),
            mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd_streaming"),
            mock.patch("agentic_ci.backends.openshell.sandbox.download", side_effect=download),
        ):
            return backend.run("prompt", "model")

    def _backend(self, tmp_path):
        backend = create_backend(
            "openshell", harness=_agent_harness(), workdir=str(tmp_path), sandbox_profile=GO
        )
        backend.run_dir = tmp_path / "_run"
        backend.toolchain_results = _RESULTS
        return backend

    def test_a_directory_left_at_the_path_is_replaced(self, tmp_path):
        backend = self._backend(tmp_path)

        def download(sandbox_path, local_dest):
            planted = tmp_path / "_run" / "toolchains.json" / "nested"
            planted.mkdir(parents=True)
            (planted / "file").write_text("x")

        assert self._run_with(backend, download) == 7
        assert json.loads((tmp_path / "_run" / "toolchains.json").read_text()) == [
            r.to_dict() for r in _RESULTS
        ]

    def test_a_symlink_left_at_the_path_is_not_written_through(self, tmp_path):
        backend = self._backend(tmp_path)
        outside = tmp_path / "outside.json"
        outside.write_text("keep")

        def download(sandbox_path, local_dest):
            (tmp_path / "_run").mkdir()
            (tmp_path / "_run" / "toolchains.json").symlink_to(outside)

        assert self._run_with(backend, download) == 7
        path = tmp_path / "_run" / "toolchains.json"
        assert not path.is_symlink()
        assert json.loads(path.read_text()) == [r.to_dict() for r in _RESULTS]
        assert outside.read_text() == "keep"

    def test_a_run_dir_left_as_a_file_keeps_the_exit_code(self, tmp_path, capsys):
        backend = self._backend(tmp_path)

        def download(sandbox_path, local_dest):
            (tmp_path / "_run").write_text("not a directory")

        assert self._run_with(backend, download) == 7
        out = capsys.readouterr().out
        assert "WARNING: toolchains.json could not be written" in out
        assert "toolchains.json write error: FileExistsError" in out
        assert (tmp_path / "_run").read_text() == "not a directory"

    def test_a_failed_rewrite_does_not_hide_the_download_error(self, tmp_path, capsys):
        backend = self._backend(tmp_path)
        error = subprocess.CalledProcessError(1, ["openshell"])

        def download(sandbox_path, local_dest):
            (tmp_path / "_run").write_text("not a directory")
            raise error

        with pytest.raises(subprocess.CalledProcessError) as raised:
            self._run_with(backend, download)
        assert raised.value is error
        assert "WARNING: toolchains.json could not be written" in capsys.readouterr().out

    @pytest.mark.parametrize("profile", [None, SandboxProfile(egress=("npm",))])
    def test_without_toolchains_the_download_is_left_alone(self, tmp_path, profile):
        kwargs = {} if profile is None else {"sandbox_profile": profile}
        backend = create_backend(
            "openshell", harness=_agent_harness(), workdir=str(tmp_path), **kwargs
        )
        backend.run_dir = tmp_path / "_run"
        with mock.patch.object(toolchains, "write_results") as write_results:
            self._run(backend, tmp_path, forge=False)
        write_results.assert_not_called()
        assert not (tmp_path / "_run").exists()


_SANDBOX_EXEC = [
    "openshell",
    "sandbox",
    "exec",
    "--name",
    "ci",
    "--no-tty",
    "--no-login-shell",
    "--",
]
_PYTHON = ["/usr/bin/python3", "-I", "-S", "-c"]
_EXEC_PYTHON = _SANDBOX_EXEC + _PYTHON


def _fake_time(clock=None):
    """A stand-in for the ``time`` module as ``sandbox`` sees it.

    The process-wide ``time`` module is left alone, so nothing else (a
    pytest-timeout thread, say) consumes the fake ticks.
    """
    ticks = iter(clock if clock is not None else range(0, 1000, 2))
    return mock.Mock(monotonic=mock.Mock(side_effect=lambda: next(ticks)), sleep=mock.Mock())


class TestMainProcess:
    def test_record_round_trip(self):
        assert MAIN.to_record() == {"pid": 19, "start_time": 214392511}
        assert sandbox.MainProcess.from_record(MAIN.to_record()) == MAIN

    @pytest.mark.parametrize(
        "record",
        [
            None,
            [],
            {},
            {"pid": 19},
            {"pid": "19", "start_time": 1},
            {"pid": 19, "start_time": 1.5},
            {"pid": True, "start_time": 1},
            {"pid": 0, "start_time": 1},
            {"pid": 19, "start_time": -1},
        ],
    )
    def test_unusable_records_are_none(self, record):
        assert sandbox.MainProcess.from_record(record) is None


class TestFindMainProcess:
    def test_runs_the_scan_with_the_image_python_and_no_login_shell(self):
        out = _completed(stdout="CANDIDATES 1\nMAIN 19 214392511\n")
        with mock.patch.object(sandbox.subprocess, "run", return_value=out) as run:
            assert sandbox.find_main_process() == MAIN
        argv = run.call_args.args[0]
        # --no-login-shell: under "bash -lc" the agent-writable ~/.bash_profile
        # could replace /usr/bin/python3 with a shell function.
        assert argv[: len(_EXEC_PYTHON)] == _EXEC_PYTHON
        assert argv[len(_EXEC_PYTHON)] == sandbox._PROCESS_SCRIPT
        assert argv[len(_EXEC_PYTHON) + 1 :] == ["main"]
        assert run.call_args.kwargs["timeout"] == sandbox._PROCESS_EXEC_TIMEOUT_SECONDS

    def test_the_script_is_not_logged(self, capsys):
        out = _completed(stdout="MAIN 19 214392511\n")
        with mock.patch.object(sandbox.subprocess, "run", return_value=out):
            sandbox.find_main_process()
        logged = capsys.readouterr().out
        assert "<process scan> main" in logged
        assert "def lineage" not in logged

    def test_waits_for_the_entrypoint_to_appear(self):
        results = [
            _completed(3, stdout="CANDIDATES 0\nNO_MAIN\n"),
            _completed(3, stdout="CANDIDATES 0\nNO_MAIN\n"),
            _completed(stdout="CANDIDATES 1\nMAIN 19 214392511\n"),
        ]
        fake_time = _fake_time()
        with (
            mock.patch.object(sandbox.subprocess, "run", side_effect=results) as run,
            mock.patch.object(sandbox, "time", fake_time),
        ):
            assert sandbox.find_main_process() == MAIN
        assert run.call_count == 3
        assert fake_time.sleep.call_count == 2

    def test_gives_up_on_no_main_after_the_bound(self):
        no_main = _completed(3, stdout="CANDIDATES 0\nNO_MAIN\n")
        with (
            mock.patch.object(sandbox.subprocess, "run", return_value=no_main) as run,
            mock.patch.object(sandbox, "time", _fake_time(range(0, 1000, 4))),
            pytest.raises(RuntimeError, match="main process: no process was found"),
        ):
            sandbox.find_main_process()
        assert run.call_count == 3

    @pytest.mark.parametrize(
        ("result", "reason"),
        [
            (_completed(5, stdout="CANDIDATES 2\nAMBIGUOUS\n"), "more than one candidate"),
            (_completed(6, stdout="CANDIDATES 1\nNOT_SLEEP\n"), "not the sandbox's sleep"),
            (_completed(stdout="MAIN nineteen 1\n"), "unusable scan output"),
            (_completed(stdout=""), "unusable scan output"),
            (_completed(1, stderr="transport error"), "exited with status 1"),
            # A supervisor too old for --no-login-shell refuses the exec.
            (
                _completed(1, stderr="sandbox supervisor is too old to honor --no-login-shell"),
                "exited with status 1",
            ),
        ],
    )
    def test_fails_closed_without_retrying(self, result, reason, capsys):
        with (
            mock.patch.object(sandbox.subprocess, "run", return_value=result) as run,
            pytest.raises(RuntimeError, match="main process") as exc,
        ):
            sandbox.find_main_process()
        assert reason in str(exc.value)
        assert run.call_count == 1
        assert "transport error" not in str(exc.value)
        assert "too old" not in str(exc.value)

    def test_a_timeout_fails_closed(self):
        timeout = subprocess.TimeoutExpired(cmd="openshell", timeout=60)
        with (
            mock.patch.object(sandbox.subprocess, "run", side_effect=timeout),
            pytest.raises(RuntimeError, match=r"timed out \(TimeoutExpired\)"),
        ):
            sandbox.find_main_process()


class TestStopLeftoverProcesses:
    def test_passes_the_main_process_and_a_deadline(self, capsys):
        out = _completed(
            stdout="KILLED pid=143 comm='sleep'\nKILLED pid=145 comm='sleep'\nLEFT 0\n"
        )
        with mock.patch.object(sandbox.subprocess, "run", return_value=out) as run:
            sandbox.stop_leftover_processes(MAIN, "validate")
        argv = run.call_args.args[0]
        assert argv[: len(_EXEC_PYTHON)] == _EXEC_PYTHON
        assert argv[len(_EXEC_PYTHON)] == sandbox._PROCESS_SCRIPT
        assert argv[len(_EXEC_PYTHON) + 1 :] == [
            "kill",
            "19",
            "214392511",
            str(sandbox._KILL_DEADLINE_SECONDS),
        ]
        logged = capsys.readouterr().out
        assert "Stopped 2 leftover sandbox process(es) before the validate phase" in logged
        # Names go to the job log only.
        assert "KILLED pid=143 comm='sleep'" in logged

    def test_nothing_left_is_fine(self):
        with mock.patch.object(sandbox.subprocess, "run", return_value=_completed(stdout="LEFT 0")):
            sandbox.stop_leftover_processes(MAIN, "setup")

    def test_a_stale_main_process_record_fails_closed(self):
        # The supervisor exits with its entrypoint, so a live sandbox without
        # the recorded main process has a stale record; the scan kills nothing.
        out = _completed(7, stdout="MAIN_GONE\n")
        with (
            mock.patch.object(sandbox.subprocess, "run", return_value=out),
            pytest.raises(RuntimeError, match="recorded main process is not running") as exc,
        ):
            sandbox.stop_leftover_processes(MAIN, "agent")
        assert "agentic-ci stop" in str(exc.value)

    @pytest.mark.parametrize(
        ("result", "reason"),
        [
            # A survivor: the scan exits 4 and names it.
            (
                _completed(4, stdout="KILLED pid=77 comm='evil\\x1b[31m'\nLEFT 1\n"),
                "1 process(es) survived",
            ),
            (_completed(4, stdout="KILLED pid=77 comm='evil'\n"), "some process(es) survived"),
            # The scan itself was killed by a leftover.
            (_completed(137, stdout="KILLED pid=77 comm='evil'\n"), "exited with status 137"),
            # Output without the final count.
            (_completed(stdout="KILLED pid=77 comm='evil'\n"), "unusable scan output"),
            (_completed(stdout="LEFT 1\n"), "unusable scan output"),
        ],
    )
    def test_anything_left_fails_closed_without_names(self, result, reason):
        with (
            mock.patch.object(sandbox.subprocess, "run", return_value=result),
            pytest.raises(RuntimeError, match="before the setup phase") as exc,
        ):
            sandbox.stop_leftover_processes(MAIN, "setup")
        assert reason in str(exc.value)
        assert "evil" not in str(exc.value)
        assert "pid" not in str(exc.value)

    def test_a_stopped_scan_times_out_and_fails_closed(self):
        timeout = subprocess.TimeoutExpired(cmd="openshell", timeout=60)
        with (
            mock.patch.object(sandbox.subprocess, "run", side_effect=timeout),
            pytest.raises(RuntimeError, match="before the validate phase"),
        ):
            sandbox.stop_leftover_processes(MAIN, "validate")

    def test_the_scripts_compile(self):
        compile(sandbox._PROCESS_SCRIPT, "<process scan>", "exec")
        compile(sandbox._PROVIDER_ENV_SCRIPT, "<provider probe>", "exec")

    def test_the_script_never_signals_a_process_group(self):
        # kill(-pgid) for the exec's group (1) would be kill(-1): every process.
        script = sandbox._PROCESS_SCRIPT
        assert "killpg" not in script
        assert "os.kill(pid, sig)" in script
        assert "os.kill(-" not in script


def _run_scan(*args, **kwargs):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", sandbox._PROCESS_SCRIPT, *args],
        capture_output=True,
        text=True,
        timeout=30,
        **kwargs,
    )


def _start_time(pid):
    with open(f"/proc/{pid}/stat", "rb") as fh:
        return int(fh.read().rpartition(b")")[2].split()[19])


def _unshare_works():
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return False
    try:
        probe = subprocess.run(
            ["unshare", "-rpf", "--mount-proc", "true"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
class TestProcessScriptMainMode:
    """Runs the scan's read-only ``main`` mode as a sibling of test children.

    The scan's parent is this test process, as the supervisor is the parent
    of an exec, so the children started here are the candidates.
    """

    @pytest.fixture
    def children(self):
        started = []

        def start(*argv):
            proc = subprocess.Popen(argv)
            started.append(proc)
            return proc

        yield start
        for proc in started:
            proc.kill()
            proc.wait()

    def test_finds_the_only_sleep_infinity(self, children):
        main = children("sleep", "infinity")
        result = _run_scan("main")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.splitlines() == [
            "CANDIDATES 1",
            "CMDLINE b'sleep\\x00infinity\\x00'",
            f"MAIN {main.pid} {_start_time(main.pid)}",
        ]

    def test_two_candidates_are_ambiguous(self, children):
        children("sleep", "infinity")
        children("sleep", "infinity")
        result = _run_scan("main")
        assert result.returncode == 5
        assert result.stdout.splitlines() == ["CANDIDATES 2", "AMBIGUOUS"]

    def test_a_candidate_that_is_not_sleep_infinity_is_refused(self, children):
        children("sleep", "3600")
        result = _run_scan("main")
        assert result.returncode == 6
        assert result.stdout.splitlines() == [
            "CANDIDATES 1",
            "CMDLINE b'sleep\\x003600\\x00'",
            "NOT_SLEEP",
        ]

    @pytest.mark.parametrize(
        ("data", "ok"),
        [
            (b"sleep\0infinity\0", True),
            (b"/usr/bin/sleep\0infinity\0", True),
            # What the Hummingbird sandbox images run: /usr/sbin/sleep is a
            # multi-call coreutils shebang script.
            (
                b"/usr/bin/coreutils\0--coreutils-prog-shebang=sleep\0/usr/sbin/sleep\0infinity\0",
                True,
            ),
            (b"sleep infinity\0", False),
            (b"sleep\0infinity\0x\0", False),
            (b"sleep\x003600\0", False),
            (b"/usr/bin/coreutils\0--coreutils-prog-shebang=cat\0/usr/sbin/cat\0infinity\0", False),
            (b"/tmp/evil\0--coreutils-prog-shebang=sleep\0/usr/sbin/sleep\0infinity\0", False),
            (b"", False),
        ],
    )
    def test_is_sleep_infinity(self, data, ok):
        helpers: dict = {}
        exec(sandbox._PROCESS_HELPERS, helpers)
        assert helpers["is_sleep_infinity"](data) is ok

    def test_a_zombie_is_no_candidate(self, children):
        main = children("sleep", "infinity")
        zombie = children("true")
        # Not reaped yet: a zombie until the fixture waits for it.
        for _ in range(100):
            with open(f"/proc/{zombie.pid}/stat", "rb") as fh:
                if fh.read().rpartition(b")")[2].split()[0] == b"Z":
                    break
            time.sleep(0.05)
        result = _run_scan("main")
        assert result.returncode == 0, result.stdout
        assert f"MAIN {main.pid} " in result.stdout


# The scan's kill mode would kill the developer's own processes on the host,
# so it runs only as PID 1's child in a new user and PID namespace, where the
# user's processes are the ones started below. Its exec parent is that PID 1.
_KILL_HARNESS = r"""
sleep infinity &
main=$!
nohup sleep 3601 >/dev/null 2>&1 &
setsid -f sleep 3602 >/dev/null 2>&1
( sleep 3603 & ) >/dev/null 2>&1
bash -c 'exec -a "sleep infinity" sleep 3604' &
sleep 0.3
start=$(cut -d')' -f2- "/proc/$main/stat" | awk '{print $20}')
if [ "$MODE" = stale ]; then start=$((start + 1)); fi
"$PY" -I -S -c "$SCRIPT" kill "$main" "$start" 5
echo "RC $?"
for pid in $(ls /proc | grep -E '^[0-9]+$'); do
    [ "$pid" = "$$" ] && continue
    state=$(cut -d')' -f2- "/proc/$pid/stat" 2>/dev/null | awk '{print $1}')
    [ -n "$state" ] && [ "$state" != Z ] && echo "ALIVE $pid $(tr '\0' ' ' < "/proc/$pid/cmdline")"
done
echo "MAINPID $main"
"""


@pytest.mark.skipif(not _unshare_works(), reason="needs unshare -rpf --mount-proc")
class TestProcessScriptKillMode:
    def _run(self, mode):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PY": sys.executable,
            "SCRIPT": sandbox._PROCESS_SCRIPT,
            "MODE": mode,
        }
        result = subprocess.run(
            ["unshare", "-rpf", "--mount-proc", "bash", "-c", _KILL_HARNESS],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        lines = result.stdout.splitlines()
        main = next(line.split()[1] for line in lines if line.startswith("MAINPID "))
        alive = {
            line.split()[1]: line.split(maxsplit=2)[2]
            for line in lines
            if line.startswith("ALIVE ")
        }
        return lines, main, alive

    def test_kills_everything_but_the_main_process_and_the_lineage(self):
        lines, main, alive = self._run("current")
        assert "RC 0" in lines, lines
        assert "LEFT 0" in lines
        killed = [line for line in lines if line.startswith("KILLED ")]
        # nohup, setsid, orphaned and look-alike leftovers.
        assert len(killed) == 4, lines
        # Only the main process is left (the listing skips PID 1, the harness
        # shell and the scan's parent, which the scan spares).
        assert set(alive) == {main}, alive
        assert alive[main].strip() == "sleep infinity"

    def test_a_stale_record_kills_nothing(self):
        lines, main, alive = self._run("stale")
        assert "MAIN_GONE" in lines
        assert "RC 7" in lines
        assert not any(line.startswith("KILLED ") for line in lines)
        assert main in alive
        assert any("sleep 3601" in cmd for cmd in alive.values())


class TestProviderAttachment:
    @pytest.mark.parametrize(
        ("func", "action"),
        [(sandbox.detach_provider, "detach"), (sandbox.attach_provider, "attach")],
    )
    def test_argv(self, func, action):
        with mock.patch.object(sandbox, "_run", return_value=_completed()) as run:
            func("setup")
        assert run.call_args.args[0] == [
            "openshell",
            "sandbox",
            "provider",
            action,
            "--wait",
            "--timeout",
            str(sandbox._PROVIDER_COMMAND_WAIT_SECONDS),
            sandbox.SANDBOX_NAME,
            PROVIDER_NAME,
        ]
        # The process bound outlasts the --wait bound, so a slow supervisor
        # is reported by openshell rather than cut off.
        assert run.call_args.kwargs["timeout"] == sandbox._PROVIDER_COMMAND_TIMEOUT_SECONDS
        assert sandbox._PROVIDER_COMMAND_TIMEOUT_SECONDS > sandbox._PROVIDER_COMMAND_WAIT_SECONDS

    @pytest.mark.parametrize(
        ("func", "action"),
        [(sandbox.detach_provider, "detach"), (sandbox.attach_provider, "attach")],
    )
    def test_failure_raises_without_stderr(self, func, action, capsys):
        failed = _completed(1, stderr="sandbox was modified by another operation")
        with (
            mock.patch.object(sandbox, "_run", return_value=failed),
            pytest.raises(RuntimeError, match=f"Could not {action}.*status 1") as exc,
        ):
            func("validate")
        assert "modified" not in str(exc.value)
        assert "modified" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("func", "action"),
        [(sandbox.detach_provider, "detach"), (sandbox.attach_provider, "attach")],
    )
    def test_a_gateway_that_does_not_answer_fails_closed(self, func, action):
        timeout = subprocess.TimeoutExpired(cmd="openshell", timeout=15, stderr="gateway said x")
        with (
            mock.patch.object(sandbox.subprocess, "run", side_effect=timeout),
            pytest.raises(
                RuntimeError,
                match=rf"Could not {action} the credential provider for the setup phase: "
                rf"openshell sandbox provider {action} timed out \(TimeoutExpired\)$",
            ) as exc,
        ):
            func("setup")
        assert "gateway said" not in str(exc.value)

    @pytest.mark.parametrize(
        ("value", "state"),
        [
            ("openshell:resolve:env:v123_OPENAI_API_KEY", "ATTACHED"),
            ("sk-real-looking-value", "DETACHED"),
            ("", "DETACHED"),
            (None, "DETACHED"),
        ],
    )
    def test_the_probe_script_reports_only_the_state(self, value, state):
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        if value is not None:
            env["OPENAI_API_KEY"] = value
        out = subprocess.run(
            [sys.executable, "-I", "-S", "-c", sandbox._PROVIDER_ENV_SCRIPT, "OPENAI_API_KEY"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout
        assert out == f"{state}\n"

    @pytest.mark.parametrize(
        ("sa_token", "adc_token", "state"),
        [
            ("openshell:resolve:env:v1_GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN", None, "ATTACHED"),
            (None, "openshell:resolve:env:v1_GOOGLE_VERTEX_AI_TOKEN", "ATTACHED"),
            ("ya29.real-looking", None, "DETACHED"),
            (None, None, "DETACHED"),
        ],
    )
    def test_the_probe_script_checks_every_variable(self, sa_token, adc_token, state):
        # A Vertex provider holds one of two tokens; either placeholder counts.
        names = ("GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN", "GOOGLE_VERTEX_AI_TOKEN")
        env = {k: v for k, v in os.environ.items() if k not in names}
        for name, value in zip(names, (sa_token, adc_token)):
            if value is not None:
                env[name] = value
        out = subprocess.run(
            [sys.executable, "-I", "-S", "-c", sandbox._PROVIDER_ENV_SCRIPT, *names],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout
        assert out == f"{state}\n"

    @pytest.mark.parametrize("env_vars", ["OPENAI_API_KEY", (), []])
    def test_a_bare_string_or_no_variable_is_refused(self, env_vars):
        # A string would be probed character by character.
        with mock.patch.object(sandbox.subprocess, "run") as run:
            with pytest.raises(ValueError, match="non-empty sequence"):
                sandbox.provider_env_state(env_vars)
            with pytest.raises(ValueError, match="non-empty sequence"):
                sandbox.wait_for_provider_env(env_vars, attached=True, phase="setup")
        run.assert_not_called()

    @pytest.mark.parametrize(
        ("result", "state"),
        [
            (_completed(stdout="ATTACHED\n"), "ATTACHED"),
            (_completed(stdout="DETACHED\n"), "DETACHED"),
            (_completed(stdout="LEFT 0\nDETACHED\n"), None),
            (_completed(1, stderr="sandbox supervisor is too old"), None),
        ],
    )
    def test_provider_env_state(self, result, state):
        with mock.patch.object(sandbox.subprocess, "run", return_value=result) as run:
            assert sandbox.provider_env_state(("OPENAI_API_KEY",), timeout=7) == state
        argv = run.call_args.args[0]
        assert argv == [*_EXEC_PYTHON, sandbox._PROVIDER_ENV_SCRIPT, "OPENAI_API_KEY"]
        assert run.call_args.kwargs["timeout"] == 7


class TestWaitForProviderEnv:
    def _wait(self, states, *, attached, clock=None):
        results = [_completed(stdout=f"{s}\n") if isinstance(s, str) else s for s in states]
        fake_time = _fake_time(clock)
        with (
            mock.patch.object(sandbox.subprocess, "run", side_effect=results) as run,
            mock.patch.object(sandbox, "time", fake_time),
        ):
            sandbox.wait_for_provider_env(("OPENAI_API_KEY",), attached=attached, phase="setup")
        return run, fake_time.sleep

    def test_polls_until_the_detach_is_seen(self):
        run, sleep = self._wait(["ATTACHED", "ATTACHED", "DETACHED"], attached=False)
        assert run.call_count == 3
        assert sleep.call_count == 2
        argv = run.call_args.args[0]
        assert argv == [*_EXEC_PYTHON, sandbox._PROVIDER_ENV_SCRIPT, "OPENAI_API_KEY"]

    def test_polls_until_the_attach_is_seen(self):
        run, _ = self._wait(["DETACHED", "ATTACHED"], attached=True)
        assert run.call_count == 2

    def test_a_failed_or_odd_probe_is_retried(self):
        timeout = subprocess.TimeoutExpired(cmd="openshell", timeout=60)
        run, _ = self._wait(
            [_completed(1, stderr="x"), timeout, "garbage", "DETACHED"], attached=False
        )
        assert run.call_count == 4

    def test_each_probe_gets_only_the_time_left(self):
        # The wait starts at t=0; probes start at t=5, 15 and 25.
        clock = iter(range(0, 1000, 5))
        fake_time = mock.Mock(monotonic=mock.Mock(side_effect=lambda: next(clock)))
        with (
            mock.patch.object(
                sandbox.subprocess, "run", return_value=_completed(stdout="ATTACHED\n")
            ) as run,
            mock.patch.object(sandbox, "time", fake_time),
            pytest.raises(RuntimeError),
        ):
            sandbox.wait_for_provider_env(("OPENAI_API_KEY",), attached=False, phase="setup")
        timeouts = [c.kwargs["timeout"] for c in run.call_args_list]
        # 30 s bound: never the full exec timeout, never below the minimum.
        assert timeouts[0] == sandbox._PROVIDER_WAIT_SECONDS - 5
        assert all(
            sandbox._PROVIDER_PROBE_MIN_TIMEOUT_SECONDS <= t <= sandbox._PROVIDER_WAIT_SECONDS
            for t in timeouts
        )
        assert timeouts[-1] == sandbox._PROVIDER_PROBE_MIN_TIMEOUT_SECONDS

    @pytest.mark.parametrize(("attached", "verb"), [(False, "stops"), (True, "resumes")])
    def test_gives_up_after_the_bound_and_fails_closed(self, attached, verb):
        stuck = "DETACHED" if attached else "ATTACHED"
        with pytest.raises(RuntimeError, match=f"injection {verb} for the setup phase within 30s"):
            self._wait([stuck] * 20, attached=attached, clock=range(0, 1000, 10))
