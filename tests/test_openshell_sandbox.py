"""Tests for OpenShell sandbox creation."""

from contextlib import ExitStack
from unittest import mock

import pytest

from agentic_ci.backends import create_backend
from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.backends.openshell.provider import PROVIDER_NAME
from agentic_ci.backends.openshell.sandbox import SANDBOX_NAME, create


@pytest.fixture
def created():
    """Create a sandbox and return the argv passed to `openshell sandbox create`."""

    def _create(**kwargs):
        with mock.patch("agentic_ci.backends.openshell.sandbox._run") as mock_run:
            with mock.patch("agentic_ci.backends.openshell.sandbox._apply_policy"):
                create(**kwargs)
        return mock_run.call_args_list[0].args[0]

    return _create


class TestCreateResourceFlags:
    def test_no_resource_flags_by_default(self, created):
        """OpenShell's own defaults apply unless a caller asks for something."""
        args = created()

        assert args == [
            "openshell",
            "sandbox",
            "create",
            "--name",
            SANDBOX_NAME,
            "--no-tty",
            "--no-auto-providers",
            "--provider",
            PROVIDER_NAME,
            "--",
            "true",
        ]

    def test_memory_limit_is_passed(self, created):
        assert "--memory" in created(memory="8Gi")
        assert "8Gi" in created(memory="8Gi")

    def test_cpu_limit_is_passed(self, created):
        args = created(cpu="2.5")

        assert args[args.index("--cpu") + 1] == "2.5"

    def test_gpu_count_is_passed(self, created):
        """Without this an accelerator on the host is invisible to the agent even
        when the container running agentic-ci can see it."""
        args = created(gpu=1)

        assert args[args.index("--gpu") + 1] == "1"

    def test_resource_flags_precede_the_command_terminator(self, created):
        """Anything after `--` is an argument to the sandbox command, not to
        OpenShell, so a flag placed there is accepted and silently ignored."""
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
        """`--memory ''` is not a smaller sandbox, and `--gpu 0` is not "no GPU"
        -- both are malformed requests where the caller meant "don't ask"."""
        args = created(memory=value, cpu=value, gpu=value)

        assert "--memory" not in args
        assert "--cpu" not in args
        assert "--gpu" not in args


class TestBackendPassesResourcesThrough:
    def test_backend_forwards_its_resource_settings(self):
        """A setting the backend accepts and never passes on is worse than one it
        rejects: the caller has no way to tell it did nothing."""
        harness = mock.Mock(auth_mode="vertex")
        harness.sandbox_config_mounts.return_value = []
        backend = OpenShellBackend(harness=harness, memory="8Gi", cpu="4", gpu=1)

        with ExitStack() as patches:
            for target in ("gateway.is_running", "provider.setup", "sandbox.upload"):
                patches.enter_context(mock.patch(f"agentic_ci.backends.openshell.{target}"))
            # Explicitly False: a bare Mock is truthy, and setup() returns early
            # on an existing sandbox without ever reaching create().
            patches.enter_context(
                mock.patch("agentic_ci.backends.openshell.sandbox.exists", return_value=False)
            )
            patches.enter_context(mock.patch.object(backend, "_run_setup_steps"))
            mock_create = patches.enter_context(
                mock.patch("agentic_ci.backends.openshell.sandbox.create")
            )
            backend.setup()

        assert mock_create.call_args.kwargs["memory"] == "8Gi"
        assert mock_create.call_args.kwargs["cpu"] == "4"
        assert mock_create.call_args.kwargs["gpu"] == 1

    def test_create_backend_accepts_the_resource_kwargs(self):
        backend = create_backend(
            "openshell",
            harness=mock.Mock(auth_mode="vertex"),
            memory="8Gi",
            cpu="4",
            gpu=1,
        )

        assert (backend.memory, backend.cpu, backend.gpu) == ("8Gi", "4", 1)

    def test_a_backend_asked_for_nothing_passes_nothing(self):
        backend = create_backend("openshell", harness=mock.Mock(auth_mode="vertex"))

        assert (backend.memory, backend.cpu, backend.gpu) == (None, None, None)
