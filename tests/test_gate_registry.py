"""Tests for the gate registry and CLI gate integration."""

import logging
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agentic_ci.gates import (
    GATE_REGISTRY,
    gitleaks_scan,
    resolve_gates,
    validate_gate_env,
)
from agentic_ci.git import GitDiffError

# A secret-looking value planted in stderr or exception text.  Gate error
# strings must never contain it; the job log must keep it.
SECRET = "glpat-PLANTEDsecretTOKEN1234567890"


class TestGateRegistry:
    def test_built_in_gates_registered(self):
        assert "sensitive-files" in GATE_REGISTRY
        assert "commit-author" in GATE_REGISTRY
        assert "gitleaks" in GATE_REGISTRY

    def test_sensitive_files_is_post(self):
        assert GATE_REGISTRY["sensitive-files"].phase == "post"

    def test_commit_author_requires_bot_email(self):
        assert "BOT_EMAIL" in GATE_REGISTRY["commit-author"].required_env

    def test_gitleaks_has_no_required_env(self):
        assert GATE_REGISTRY["gitleaks"].required_env == []


class TestResolveGates:
    def test_resolve_known_gates(self):
        specs = resolve_gates(["sensitive-files", "gitleaks"])
        assert len(specs) == 2
        assert specs[0].name == "sensitive-files"
        assert specs[1].name == "gitleaks"

    def test_resolve_unknown_gate_exits(self):
        with pytest.raises(SystemExit, match="unknown gate"):
            resolve_gates(["nonexistent-gate"])


class TestValidateGateEnv:
    def test_all_vars_present(self):
        gates = [GATE_REGISTRY["commit-author"]]
        with patch.dict(os.environ, {"BOT_EMAIL": "bot@ci.com"}):
            validate_gate_env(gates)

    def test_missing_var_exits(self):
        gates = [GATE_REGISTRY["commit-author"]]
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit, match="BOT_EMAIL"):
                validate_gate_env(gates)

    def test_reports_all_missing_at_once(self):
        gates = [GATE_REGISTRY["commit-author"], GATE_REGISTRY["sensitive-files"]]
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                validate_gate_env(gates)
            msg = str(exc_info.value)
            assert "BOT_EMAIL" in msg

    def test_no_required_env_passes(self):
        gates = [GATE_REGISTRY["sensitive-files"]]
        with patch.dict(os.environ, {}, clear=True):
            validate_gate_env(gates)


class TestRunSensitiveFiles:
    def test_no_changes_passes(self):
        gate = GATE_REGISTRY["sensitive-files"]
        with patch("agentic_ci.gates.get_changed_files", return_value=[]):
            errors = gate.fn(workdir="/tmp/test")
        assert errors == []

    def test_sensitive_file_blocked(self):
        gate = GATE_REGISTRY["sensitive-files"]
        with patch("agentic_ci.gates.get_changed_files", return_value=[".env", "main.py"]):
            errors = gate.fn(workdir="/tmp/test")
        assert len(errors) == 1
        assert ".env" in errors[0]

    def test_git_diff_error_text_stays_in_log(self, caplog):
        gate = GATE_REGISTRY["sensitive-files"]
        with (
            caplog.at_level(logging.ERROR, logger="agentic_ci.gates"),
            patch(
                "agentic_ci.gates.get_changed_files",
                side_effect=GitDiffError(f"git diff failed: fatal: {SECRET}"),
            ),
        ):
            errors = gate.fn(workdir="/tmp/test")
        assert errors == [
            "Could not compute changed files (GitDiffError); see the CI job log",
        ]
        assert SECRET in caplog.text


class TestRunCommitAuthor:
    def test_matching_author_passes(self):
        gate = GATE_REGISTRY["commit-author"]
        with (
            patch.dict(os.environ, {"BOT_EMAIL": "bot@ci.com"}),
            patch(
                "agentic_ci.gates.get_commit_info",
                return_value={"email": "bot@ci.com", "subject": "fix"},
            ),
        ):
            errors = gate.fn(workdir="/tmp/test")
        assert errors == []

    def test_wrong_author_fails(self):
        gate = GATE_REGISTRY["commit-author"]
        with (
            patch.dict(os.environ, {"BOT_EMAIL": "bot@ci.com"}),
            patch(
                "agentic_ci.gates.get_commit_info",
                return_value={"email": "human@ci.com", "subject": "fix"},
            ),
        ):
            errors = gate.fn(workdir="/tmp/test")
        assert len(errors) == 1
        assert "human@ci.com" in errors[0]

    def test_git_log_error_text_stays_in_log(self, caplog):
        gate = GATE_REGISTRY["commit-author"]
        exc = subprocess.CalledProcessError(128, ["git", "log", SECRET], stderr=f"fatal: {SECRET}")
        with (
            caplog.at_level(logging.ERROR, logger="agentic_ci.gates"),
            patch.dict(os.environ, {"BOT_EMAIL": "bot@ci.com"}),
            patch("agentic_ci.gates.get_commit_info", side_effect=exc),
        ):
            errors = gate.fn(workdir="/tmp/test")
        assert errors == ["Could not read commit info (CalledProcessError); see the CI job log"]
        assert f"fatal: {SECRET}" in caplog.text


class TestGitleaksScan:
    def test_missing_binary_fails_closed(self, tmp_path):
        with patch("shutil.which", return_value=None):
            errors = gitleaks_scan(tmp_path)
        assert len(errors) == 1
        assert "not installed" in errors[0]

    def test_timeout_fails_closed(self, tmp_path):
        rev_list_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="3\n")
        with (
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch(
                "subprocess.run",
                side_effect=[
                    rev_list_result,
                    subprocess.TimeoutExpired(cmd="gitleaks", timeout=120),
                ],
            ),
        ):
            errors = gitleaks_scan(tmp_path)
        assert len(errors) == 1
        assert "timed out" in errors[0]

    def test_rev_list_error_stderr_stays_in_log(self, tmp_path, caplog):
        exc = subprocess.CalledProcessError(
            128, ["git", "rev-list"], stderr=f"fatal: bad revision {SECRET}\n"
        )
        with (
            caplog.at_level(logging.ERROR, logger="agentic_ci.gates"),
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch("subprocess.run", side_effect=exc),
        ):
            errors = gitleaks_scan(tmp_path)
        assert errors == [
            "gitleaks pre-check failed: git rev-list error (CalledProcessError); see the CI job log"
        ]
        assert SECRET not in errors[0]
        assert f"fatal: bad revision {SECRET}" in caplog.text

    def test_rev_list_timeout_fails_closed(self, tmp_path, caplog):
        exc = subprocess.TimeoutExpired(cmd="git", timeout=30, stderr=f"{SECRET}".encode())
        with (
            caplog.at_level(logging.ERROR, logger="agentic_ci.gates"),
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch("subprocess.run", side_effect=exc),
        ):
            errors = gitleaks_scan(tmp_path)
        assert errors == [
            "gitleaks pre-check failed: git rev-list error (TimeoutExpired); see the CI job log"
        ]
        assert SECRET in caplog.text

    def test_rev_list_os_error_fails_closed(self, tmp_path):
        with (
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file", "git")),
        ):
            errors = gitleaks_scan(tmp_path)
        assert errors == [
            "gitleaks pre-check failed: git rev-list error (FileNotFoundError); see the CI job log"
        ]


class TestRunJiraDescriptionEditors:
    ENV = {
        "TICKET_KEY": "TEST-1",
        "INTERNAL_DOMAIN_RE": r"@redhat\.com$",
    }

    def _run(self, env=None, client=None, from_env_exc=None):
        gate = GATE_REGISTRY["jira-description-editors"]
        from_env = (
            patch("agentic_ci.gates.JiraClient.from_env", side_effect=from_env_exc)
            if from_env_exc is not None
            else patch("agentic_ci.gates.JiraClient.from_env", return_value=client)
        )
        with patch.dict(os.environ, env or self.ENV, clear=True), from_env:
            return gate.fn()

    def test_invalid_pattern_error_text_stays_in_log(self, caplog):
        env = {"TICKET_KEY": "TEST-1", "INTERNAL_DOMAIN_RE": f"({SECRET}$"}
        with caplog.at_level(logging.ERROR, logger="agentic_ci.gates"):
            result = self._run(env=env, client=MagicMock())
        assert result == "Invalid INTERNAL_DOMAIN_RE pattern; see the CI job log"
        assert "missing )" in caplog.text

    def test_client_creation_error_text_stays_in_log(self, caplog):
        with caplog.at_level(logging.ERROR, logger="agentic_ci.gates"):
            result = self._run(from_env_exc=RuntimeError(f"bad token {SECRET}"))
        assert result == "Could not create Jira client (RuntimeError); see the CI job log"
        assert SECRET in caplog.text

    def test_get_issue_error_text_stays_in_log(self, caplog):
        client = MagicMock()
        client.get_issue.side_effect = RuntimeError(f"401 body {SECRET}")
        with caplog.at_level(logging.ERROR, logger="agentic_ci.gates"):
            result = self._run(client=client)
        assert result == "Could not fetch issue TEST-1 (RuntimeError); see the CI job log"
        assert SECRET in caplog.text

    def test_changelog_error_text_stays_in_log(self, caplog):
        client = MagicMock()
        client.get_issue.return_value = {"reporter_email": "dev@redhat.com"}
        client.get_description_editors.side_effect = RuntimeError(f"500 body {SECRET}")
        with caplog.at_level(logging.ERROR, logger="agentic_ci.gates"):
            result = self._run(client=client)
        assert result == "Could not fetch changelog for TEST-1 (RuntimeError); see the CI job log"
        assert SECRET in caplog.text

    def test_trusted_editors_pass(self):
        client = MagicMock()
        client.get_issue.return_value = {"reporter_email": "dev@redhat.com"}
        client.get_description_editors.return_value = ["dev@redhat.com"]
        assert self._run(client=client) is None
