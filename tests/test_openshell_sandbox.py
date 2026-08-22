"""Tests for OpenShell sandbox lifecycle commands."""

from unittest import mock

from agentic_ci.backends.openshell import sandbox


def test_create_uses_terminating_initial_command():
    with (
        mock.patch.object(sandbox, "_run") as run,
        mock.patch.object(sandbox, "_apply_policy"),
    ):
        sandbox.create(image="codex-sandbox:latest")

    create_args = run.call_args.args[0]
    assert create_args[-2:] == ["--", "true"]
