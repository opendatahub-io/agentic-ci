"""Tests for git helper functions."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from agentic_ci.git import checkout_branch, get_default_branch, git_output


class TestCheckoutBranch:
    def test_success(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            assert checkout_branch(tmp_path, "feature/test") is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["git", "checkout", "feature/test"]
            assert args[1]["cwd"] == str(tmp_path)

    def test_checkout_failure(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr="error")
            assert checkout_branch(tmp_path, "feature/test") is False

    def test_git_not_found(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert checkout_branch(tmp_path, "feature/test") is False

    def test_invalid_ref_rejected(self, tmp_path: Path):
        assert checkout_branch(tmp_path, "--evil") is False
        assert checkout_branch(tmp_path, "") is False
        assert checkout_branch(tmp_path, "foo..bar") is False

    def test_valid_ref_names(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            assert checkout_branch(tmp_path, "main") is True
            assert checkout_branch(tmp_path, "feature/my-branch") is True
            assert checkout_branch(tmp_path, "v1.0.0") is True


class TestGetDefaultBranch:
    def test_returns_default_branch(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="origin/develop\n")
            assert get_default_branch(tmp_path) == "develop"

    def test_returns_main_on_error(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr="error")
            assert get_default_branch(tmp_path) == "main"

    def test_returns_main_when_origin_head(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="origin/HEAD\n")
            assert get_default_branch(tmp_path) == "main"

    def test_returns_main_on_file_not_found(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert get_default_branch(tmp_path) == "main"

    def test_strips_origin_prefix(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="origin/master\n")
            assert get_default_branch(tmp_path) == "master"


class TestGitOutput:
    def test_returns_stdout(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="abc123\n")
            assert git_output(tmp_path, "rev-parse", "HEAD") == "abc123"
            args = mock_run.call_args
            assert args[0][0] == ["git", "rev-parse", "HEAD"]

    def test_returns_none_on_error(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr="error")
            assert git_output(tmp_path, "log") is None

    def test_returns_none_on_file_not_found(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert git_output(tmp_path, "status") is None

    def test_strips_whitespace(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="  some output  \n")
            assert git_output(tmp_path, "diff") == "some output"

    def test_multiple_args(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="output\n")
            git_output(tmp_path, "log", "--oneline", "-5")
            args = mock_run.call_args
            assert args[0][0] == ["git", "log", "--oneline", "-5"]
