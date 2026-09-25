"""Tests for OpenShell sandbox lifecycle commands."""

from unittest import mock

import pytest

import agentic_ci.backends.openshell as openshell_backend
from agentic_ci.backends import create_backend
from agentic_ci.backends.openshell import sandbox
from agentic_ci.backends.openshell.provider import PROVIDER_NAME
from agentic_ci.sandbox_profile import Resources, SandboxProfile


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
        identity = openshell_backend._sandbox_identity("claude-code", backend.image, "vertex")
        with (
            mock.patch(f"{openshell}.provider") as provider,
            mock.patch(f"{openshell}.gateway.is_running", return_value=True),
            mock.patch(f"{openshell}.sandbox.exists", return_value=exists),
            mock.patch(f"{openshell}.sandbox.create") as create,
            mock.patch(f"{openshell}.sandbox.delete") as delete,
            mock.patch(f"{openshell}.sandbox.upload"),
            mock.patch(f"{openshell}._load_sandbox_identity", return_value=identity),
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

    def test_reused_sandbox_says_profile_resources_were_not_applied(self, tmp_path, monkeypatch):
        backend, _ = self._backend(Resources(memory="8Gi", gpu=1))
        create, delete, logged = self._setup(backend, tmp_path, monkeypatch, exists=True)
        create.assert_not_called()
        delete.assert_not_called()
        warning = logged.call_args.args[0]
        assert "memory=8Gi" in warning and "gpu=1" in warning
        assert "cpu" not in warning
        assert "Delete it" in warning
