"""Tests for OpenShell sandbox policy application."""

import os
from unittest import mock

import pytest

from agentic_ci.backends.openshell import sandbox


class TestApplyPolicyUpdate:
    def test_builds_correct_args_with_binaries(self):
        with mock.patch.object(sandbox, "_run") as mock_run:
            sandbox._apply_policy_update(
                ["github.com:443:full", "pypi.org:443:read-only"],
                ["/usr/local/bin/claude", "/usr/local/bin/opencode"],
            )
        args = mock_run.call_args[0][0]
        assert args[:4] == ["openshell", "policy", "update", "--wait"]
        assert "--binary" in args
        assert "/usr/local/bin/claude" in args
        assert "/usr/local/bin/opencode" in args
        assert "--add-endpoint" in args
        assert "github.com:443:full" in args
        assert "pypi.org:443:read-only" in args
        assert args[-1] == sandbox.SANDBOX_NAME

    def test_appends_otel_endpoint(self):
        with mock.patch.object(sandbox, "_run") as mock_run:
            sandbox._apply_policy_update(
                ["github.com:443:full"],
                ["/usr/local/bin/agent"],
                otel_port=4318,
            )
        args = mock_run.call_args[0][0]
        assert "host.openshell.internal:4318:read-write" in args

    def test_no_otel_endpoint_when_port_is_none(self):
        with mock.patch.object(sandbox, "_run") as mock_run:
            sandbox._apply_policy_update(
                ["github.com:443:full"],
                ["/usr/local/bin/agent"],
            )
        args = mock_run.call_args[0][0]
        assert not any("host.openshell.internal" in a for a in args)


class TestApplyPolicyYaml:
    def test_writes_temp_file_and_calls_policy_set(self):
        with mock.patch.object(sandbox, "_run") as mock_run:
            sandbox._apply_policy_yaml(
                ["github.com:443:full"],
                ["/usr/local/bin/agent"],
                tls_skip_hosts=["api2.cursor.sh"],
            )
        args = mock_run.call_args[0][0]
        assert args[0:3] == ["openshell", "policy", "set"]
        assert args[3] == sandbox.SANDBOX_NAME
        assert "--policy" in args
        assert "--wait" in args

    def test_cleans_up_temp_file_on_success(self):
        captured_path = []

        def capture_run(args, **kwargs):
            for i, a in enumerate(args):
                if a == "--policy" and i + 1 < len(args):
                    captured_path.append(args[i + 1])
            return mock.Mock(returncode=0)

        with mock.patch.object(sandbox, "_run", side_effect=capture_run):
            sandbox._apply_policy_yaml(
                ["github.com:443:full"],
                ["/usr/local/bin/agent"],
                tls_skip_hosts=["api2.cursor.sh"],
            )
        assert len(captured_path) == 1
        assert not os.path.exists(captured_path[0])

    def test_cleans_up_temp_file_on_failure(self):
        captured_path = []

        def capture_run(args, **kwargs):
            for i, a in enumerate(args):
                if a == "--policy" and i + 1 < len(args):
                    captured_path.append(args[i + 1])
            raise Exception("openshell failed")

        with mock.patch.object(sandbox, "_run", side_effect=capture_run):
            with pytest.raises(Exception, match="openshell failed"):
                sandbox._apply_policy_yaml(
                    ["github.com:443:full"],
                    ["/usr/local/bin/agent"],
                    tls_skip_hosts=["api2.cursor.sh"],
                )

        assert len(captured_path) == 1
        assert not os.path.exists(captured_path[0])


class TestApplyPolicyDispatch:
    def test_passes_tls_skip_hosts_into_resolve_endpoints(self):
        tls_skip_hosts = [("api2.cursor.sh", 443, "read-write")]
        with (
            mock.patch.object(
                sandbox, "resolve_endpoints", return_value=["github.com:443:full"]
            ) as mock_resolve,
            mock.patch.object(sandbox, "_apply_policy_yaml") as mock_yaml,
        ):
            sandbox._apply_policy(
                policy_path=None,
                tls_skip_hosts=tls_skip_hosts,
                binaries=["/usr/local/bin/agent"],
            )
        mock_resolve.assert_called_once_with(None, workdir=".", tls_skip_hosts=tls_skip_hosts)
        mock_yaml.assert_called_once()

    def test_dispatches_to_yaml_when_tls_skip_hosts_present(self):
        with (
            mock.patch.object(sandbox, "_apply_policy_yaml") as mock_yaml,
            mock.patch.object(sandbox, "_apply_policy_update") as mock_update,
        ):
            sandbox._apply_policy(
                policy_path=None,
                tls_skip_hosts=[("api2.cursor.sh", 443, "read-write")],
                binaries=["/usr/local/bin/agent"],
            )
        mock_yaml.assert_called_once()
        mock_update.assert_not_called()

    def test_dispatches_to_update_when_no_tls_skip_hosts(self):
        with (
            mock.patch.object(sandbox, "_apply_policy_yaml") as mock_yaml,
            mock.patch.object(sandbox, "_apply_policy_update") as mock_update,
        ):
            sandbox._apply_policy(
                policy_path=None,
                tls_skip_hosts=None,
                binaries=["/usr/local/bin/claude"],
            )
        mock_update.assert_called_once()
        mock_yaml.assert_not_called()

    def test_returns_early_when_no_endpoints(self, tmp_path):
        empty_policy = tmp_path / "empty.yml"
        empty_policy.write_text("custom: true\n")
        with (
            mock.patch.object(sandbox, "_apply_policy_yaml") as mock_yaml,
            mock.patch.object(sandbox, "_apply_policy_update") as mock_update,
            mock.patch("agentic_ci.backends.openshell.sandbox.resolve_endpoints", return_value=[]),
        ):
            sandbox._apply_policy(policy_path=str(empty_policy))
        mock_yaml.assert_not_called()
        mock_update.assert_not_called()
