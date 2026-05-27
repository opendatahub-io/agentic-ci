"""Tests for the forge package public API."""

import pytest

from agentic_ci.forge import (
    Forge,
    ForgeError,
    detect_forge,
    parse_github_pr_url,
    parse_gitlab_mr_url,
    repo_path_from_url,
)


class TestForgeDetect:
    def test_gitlab_url(self):
        from agentic_ci.forge.gitlab import GitLabForge

        forge = Forge.detect("https://gitlab.com/org/repo/-/merge_requests/42")
        assert isinstance(forge, GitLabForge)

    def test_github_url(self):
        from agentic_ci.forge.github import GitHubForge

        forge = Forge.detect("https://github.com/owner/repo/pull/10", github_token="tok")
        assert isinstance(forge, GitHubForge)

    def test_unknown_host_raises(self):
        with pytest.raises(ForgeError, match="Unrecognized forge host"):
            Forge.detect("https://bitbucket.org/owner/repo")

    def test_github_passes_token(self):
        forge = Forge.detect("https://github.com/o/r/pull/1", github_token="my-token")
        assert forge._token == "my-token"


class TestDetectForgeWrapper:
    def test_delegates_to_classmethod(self):
        from agentic_ci.forge.gitlab import GitLabForge

        forge = detect_forge("https://gitlab.com/org/repo/-/merge_requests/42")
        assert isinstance(forge, GitLabForge)


class TestParseGitlabMrUrl:
    def test_valid_url(self):
        project_path, mr_iid = parse_gitlab_mr_url(
            "https://gitlab.com/my-org/my-repo/-/merge_requests/99"
        )
        assert project_path == "my-org/my-repo"
        assert mr_iid == 99

    def test_nested_group(self):
        project_path, mr_iid = parse_gitlab_mr_url("https://gitlab.com/a/b/c/-/merge_requests/1")
        assert project_path == "a/b/c"
        assert mr_iid == 1

    def test_invalid_url_raises(self):
        with pytest.raises(ForgeError, match="Invalid GitLab MR URL"):
            parse_gitlab_mr_url("https://gitlab.com/org/repo")

    def test_github_url_raises(self):
        with pytest.raises(ForgeError, match="Invalid GitLab MR URL"):
            parse_gitlab_mr_url("https://github.com/owner/repo/pull/5")


class TestParseGithubPrUrl:
    def test_valid_url(self):
        repo_path, pr_number = parse_github_pr_url("https://github.com/owner/repo/pull/42")
        assert repo_path == "owner/repo"
        assert pr_number == 42

    def test_invalid_url_raises(self):
        with pytest.raises(ForgeError, match="Invalid GitHub PR URL"):
            parse_github_pr_url("https://github.com/owner/repo")

    def test_gitlab_url_raises(self):
        with pytest.raises(ForgeError, match="Invalid GitHub PR URL"):
            parse_github_pr_url("https://gitlab.com/org/repo/-/merge_requests/1")


class TestRepoPathFromUrl:
    def test_strips_trailing_slash(self):
        assert repo_path_from_url("https://gitlab.com/org/repo/") == "org/repo"

    def test_strips_dot_git(self):
        assert repo_path_from_url("https://github.com/owner/repo.git") == "owner/repo"

    def test_strips_both(self):
        assert repo_path_from_url("https://gitlab.com/a/b/c.git") == "a/b/c"

    def test_plain_url(self):
        assert repo_path_from_url("https://github.com/owner/repo") == "owner/repo"
