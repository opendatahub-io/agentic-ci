"""Tests for branch resolution module."""

import subprocess
from unittest.mock import patch

import pytest

from agentic_ci.branch import (
    BranchResolutionError,
    resolve_branch_from_jira,
)
from agentic_ci.git import _validate_ref, validate_branch_exists


class TestValidateRef:
    """Test _validate_ref function for git ref injection protection."""

    def test_valid_names(self):
        """Valid branch names should pass validation."""
        assert _validate_ref("main") is True
        assert _validate_ref("develop") is True
        assert _validate_ref("feature/my-branch") is True
        assert _validate_ref("release/v1.0.0") is True
        assert _validate_ref("v2.3.1") is True
        assert _validate_ref("user/name/fix-123") is True
        assert _validate_ref("branch_with_underscores") is True
        assert _validate_ref("branch-with-dashes") is True
        assert _validate_ref("Branch.With.Dots") is True
        assert _validate_ref("~branch") is True
        assert _validate_ref("branch^") is True

    def test_invalid_names(self):
        """Invalid or dangerous branch names should fail validation."""
        # Empty or starting with dash
        assert _validate_ref("") is False
        assert _validate_ref("--evil") is False
        assert _validate_ref("-branch") is False

        # Injection patterns
        assert _validate_ref("foo..bar") is False
        assert _validate_ref("branch@{1}") is False
        assert _validate_ref("@{HEAD}") is False

        # Special characters that could be used for injection
        assert _validate_ref("branch;rm -rf /") is False
        assert _validate_ref("branch && ls") is False
        assert _validate_ref("branch|cat /etc/passwd") is False
        assert _validate_ref("branch\nls") is False
        assert _validate_ref("branch\0") is False


class TestValidateBranchExists:
    """Test validate_branch_exists function."""

    def test_branch_exists(self):
        """Should return True when branch exists on remote."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "ls-remote"],
                returncode=0,
                stdout="abc123\trefs/heads/main\n",
                stderr="",
            )
            assert validate_branch_exists("https://github.com/test/repo.git", "main") is True
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == [
                "git",
                "ls-remote",
                "--heads",
                "https://github.com/test/repo.git",
                "main",
            ]

    def test_branch_does_not_exist(self):
        """Should return False when branch does not exist (empty output)."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "ls-remote"],
                returncode=0,
                stdout="",
                stderr="",
            )
            result = validate_branch_exists("https://github.com/test/repo.git", "nonexistent")
            assert result is False

    def test_git_command_fails(self):
        """Should return False when git ls-remote fails."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "ls-remote"],
                returncode=128,
                stdout="",
                stderr="fatal: unable to access repository",
            )
            assert validate_branch_exists("https://github.com/test/repo.git", "main") is False

    def test_timeout(self):
        """Should return False when git ls-remote times out."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)
            assert validate_branch_exists("https://github.com/test/repo.git", "main") is False

    def test_git_not_found(self):
        """Should return False when git is not installed."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert validate_branch_exists("https://github.com/test/repo.git", "main") is False

    def test_invalid_ref_rejected(self):
        """Should return False and not call git for invalid ref names."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            assert validate_branch_exists("https://github.com/test/repo.git", "..") is False
            assert validate_branch_exists("https://github.com/test/repo.git", "--evil") is False
            mock_run.assert_not_called()

    def test_subprocess_timeout_param(self):
        """Should pass timeout=30 to subprocess.run."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "ls-remote"],
                returncode=0,
                stdout="abc123\trefs/heads/main\n",
                stderr="",
            )
            validate_branch_exists("https://github.com/test/repo.git", "main")
            assert mock_run.call_args[1]["timeout"] == 30

    def test_subprocess_stdin_devnull(self):
        """Should pass stdin=subprocess.DEVNULL to prevent blocking."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "ls-remote"],
                returncode=0,
                stdout="abc123\trefs/heads/main\n",
                stderr="",
            )
            validate_branch_exists("https://github.com/test/repo.git", "main")
            assert mock_run.call_args[1]["stdin"] == subprocess.DEVNULL


class TestResolveBranch:
    """Test resolve_branch_from_jira function."""

    def test_fixversion_only(self):
        """Should return fixVersion directly when no version_branches map exists."""
        ticket = {"fixVersions": [{"name": "v1.0.0"}]}

        component_config = {}

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "v1.0.0"

    def test_version_branches_override(self):
        """Should use version_branches map when fixVersion matches a key."""
        ticket = {"fixVersions": [{"name": "2.0"}]}

        component_config = {
            "version_branches": {
                "2.0": "release/2.0.x",
                "1.0": "release/1.0.x",
            }
        }

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "release/2.0.x"

    def test_fixversion_not_in_version_branches(self):
        """Should use fixVersion directly when not in version_branches map."""
        ticket = {"fixVersions": [{"name": "v3.0.0"}]}

        component_config = {
            "version_branches": {
                "2.0": "release/2.0.x",
                "1.0": "release/1.0.x",
            }
        }

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "v3.0.0"

    def test_static_branch_fallback(self):
        """Should fall back to component.branch when no fixVersion exists."""
        ticket = {}

        component_config = {"branch": "develop"}

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "develop"

    def test_no_resolution_returns_none(self):
        """Should return None when no branch can be resolved."""
        ticket = {}

        component_config = {}

        result = resolve_branch_from_jira(ticket, component_config)
        assert result is None

    def test_empty_fixversions_list(self):
        """Should fall back when fixVersions is an empty list."""
        ticket = {"fixVersions": []}

        component_config = {"branch": "main"}

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "main"

    def test_fixversion_empty_name(self):
        """Should fall back when fixVersion has empty name."""
        ticket = {"fixVersions": [{"name": ""}]}

        component_config = {"branch": "develop"}

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "develop"

    def test_fixversion_missing_name_key(self):
        """Should fall back when fixVersion dict is missing 'name' key."""
        ticket = {"fixVersions": [{"id": "12345"}]}

        component_config = {"branch": "main"}

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "main"

    def test_none_component_config(self):
        """Should work when component_config is None."""
        ticket = {"fixVersions": [{"name": "v2.0"}]}

        result = resolve_branch_from_jira(ticket, component_config=None)
        assert result == "v2.0"

    def test_with_repo_url_validation_succeeds(self):
        """Should return branch when validation succeeds."""
        ticket = {"fixVersions": [{"name": "v1.0"}]}

        component_config = {}

        with patch("agentic_ci.branch.validate_branch_exists") as mock_validate:
            mock_validate.return_value = True
            result = resolve_branch_from_jira(
                ticket,
                component_config,
                repo_url="https://github.com/test/repo.git",
            )

        assert result == "v1.0"
        mock_validate.assert_called_once_with("https://github.com/test/repo.git", "v1.0")

    def test_with_repo_url_validation_fails(self):
        """Should return None when validation fails."""
        ticket = {"fixVersions": [{"name": "v1.0"}]}

        component_config = {}

        with patch("agentic_ci.branch.validate_branch_exists") as mock_validate:
            mock_validate.return_value = False
            result = resolve_branch_from_jira(
                ticket,
                component_config,
                repo_url="https://github.com/test/repo.git",
            )

        assert result is None
        mock_validate.assert_called_once_with("https://github.com/test/repo.git", "v1.0")

    def test_no_repo_url_skips_validation(self):
        """Should skip validation and return branch when no repo_url provided."""
        ticket = {"fixVersions": [{"name": "v1.0"}]}

        component_config = {}

        with patch("agentic_ci.branch.validate_branch_exists") as mock_validate:
            result = resolve_branch_from_jira(ticket, component_config, repo_url=None)

        assert result == "v1.0"
        mock_validate.assert_not_called()

    def test_multiple_fixversions_uses_first(self):
        """Should use only the first fixVersion when multiple exist."""
        ticket = {
            "fixVersions": [
                {"name": "v2.0"},
                {"name": "v1.0"},
            ]
        }

        component_config = {}

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "v2.0"

    def test_version_branches_preferred_over_static_branch(self):
        """Should use version_branches override even when static branch exists."""
        ticket = {"fixVersions": [{"name": "2.0"}]}

        component_config = {
            "version_branches": {"2.0": "release/2.0.x"},
            "branch": "main",
        }

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "release/2.0.x"

    def test_fixversion_preferred_over_static_branch(self):
        """Should use fixVersion even when static branch exists."""
        ticket = {"fixVersions": [{"name": "v3.0"}]}

        component_config = {"branch": "main"}

        result = resolve_branch_from_jira(ticket, component_config)
        assert result == "v3.0"


class TestBranchResolutionError:
    """Test BranchResolutionError exception class."""

    def test_is_exception(self):
        """Should be a proper Exception subclass."""
        assert issubclass(BranchResolutionError, Exception)

    def test_can_be_raised(self):
        """Should be raiseable and catchable."""
        with pytest.raises(BranchResolutionError, match="test error"):
            raise BranchResolutionError("test error")

    def test_can_be_instantiated(self):
        """Should be instantiable with a message."""
        error = BranchResolutionError("something went wrong")
        assert str(error) == "something went wrong"
