"""Tests for git credential setup functions."""

import subprocess
from unittest.mock import MagicMock, patch

from agentic_ci.git import (
    _set_insteadof,
    _setup_github_credentials,
    _setup_gitlab_credentials,
    setup_git_credentials,
)


class TestSetupGitCredentials:
    def test_gitlab_url_calls_gitlab_setup(self, monkeypatch):
        monkeypatch.setenv("BOT_PAT", "glpat-test")
        with patch("agentic_ci.git._setup_gitlab_credentials", return_value=True) as mock_gl:
            result = setup_git_credentials("https://gitlab.com/org/repo")
        assert result is True
        mock_gl.assert_called_once()

    def test_github_url_with_resolver(self):
        resolver = MagicMock(return_value="ghp-token")
        with patch("agentic_ci.git._setup_github_credentials", return_value=True) as mock_gh:
            result = setup_git_credentials(
                "https://github.com/owner/repo",
                github_token_resolver=resolver,
            )
        assert result is True
        mock_gh.assert_called_once_with("https://github.com/owner/repo", resolver)

    def test_github_url_without_resolver_returns_false(self):
        result = setup_git_credentials("https://github.com/owner/repo")
        assert result is False

    def test_empty_url_returns_false(self):
        result = setup_git_credentials("")
        assert result is False

    def test_unknown_host_returns_true(self):
        result = setup_git_credentials("https://bitbucket.org/owner/repo")
        assert result is True


class TestSetupGitlabCredentials:
    def test_sets_insteadof_with_bot_pat(self, monkeypatch):
        monkeypatch.setenv("BOT_PAT", "glpat-secret")
        with patch("agentic_ci.git._set_insteadof", return_value=True) as mock_set:
            result = _setup_gitlab_credentials()
        assert result is True
        mock_set.assert_called_once_with(
            "https://oauth2:glpat-secret@gitlab.com/",
            "https://gitlab.com/",
        )

    def test_returns_false_without_bot_pat(self, monkeypatch):
        monkeypatch.delenv("BOT_PAT", raising=False)
        result = _setup_gitlab_credentials()
        assert result is False


class TestSetupGithubCredentials:
    def test_sets_insteadof_with_token(self):
        resolver = MagicMock(return_value="ghp-resolved-token")
        with patch("agentic_ci.git._set_insteadof", return_value=True) as mock_set:
            result = _setup_github_credentials("https://github.com/o/r", resolver)
        assert result is True
        mock_set.assert_called_once_with(
            "https://x-access-token:ghp-resolved-token@github.com/",
            "https://github.com/",
        )

    def test_returns_false_when_resolver_returns_none(self):
        resolver = MagicMock(return_value=None)
        result = _setup_github_credentials("https://github.com/o/r", resolver)
        assert result is False


class TestSetInsteadof:
    def test_runs_git_config(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = _set_insteadof(
                "https://oauth2:tok@gitlab.com/",
                "https://gitlab.com/",
            )
        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "git"
        assert args[1] == "config"
        assert args[2] == "--global"
        assert "url.https://oauth2:tok@gitlab.com/.insteadOf" in args[3]
        assert args[4] == "https://gitlab.com/"

    def test_returns_false_on_subprocess_error(self):
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            result = _set_insteadof("https://a@b.com/", "https://b.com/")
        assert result is False

    def test_returns_false_on_missing_git(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = _set_insteadof("https://a@b.com/", "https://b.com/")
        assert result is False

    def test_warns_outside_ci(self, monkeypatch, caplog):
        import logging

        monkeypatch.delenv("CI", raising=False)
        with (
            patch("subprocess.run") as mock_run,
            caplog.at_level(logging.WARNING),
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            _set_insteadof("https://a@b.com/", "https://b.com/")
        assert "persists on local machines" in caplog.text
