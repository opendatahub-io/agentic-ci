"""Tests for the gate registry and CLI gate integration."""

import os
import subprocess
from unittest.mock import patch

import pytest

from agentic_ci.gates import (
    GATE_REGISTRY,
    gitleaks_scan,
    resolve_gates,
    validate_gate_env,
)


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
