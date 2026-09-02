"""Tests for OpenShell sandbox lifecycle commands."""

from unittest import mock

from agentic_ci.backends.openshell import sandbox


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
