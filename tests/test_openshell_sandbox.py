"""Tests for OpenShell sandbox lifecycle commands."""

from unittest import mock

import pytest

from agentic_ci.backends import create_backend
from agentic_ci.backends.openshell import sandbox
from agentic_ci.backends.openshell.provider import PROVIDER_NAME


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
