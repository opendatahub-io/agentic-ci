"""Tests for the forge package public API."""

import pytest

from agentic_ci.forge import (
    GITLAB_DEVELOPER_ACCESS_LEVEL,
    MIN_TRUSTED_GITLAB_ACCESS_LEVEL,
    TRUSTED_GITHUB_ASSOCIATIONS,
    Forge,
    ForgeError,
    detect_forge,
    filter_trusted_comments,
    filter_trusted_threads,
    is_trusted_comment,
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


class TestTrustThresholds:
    def test_github_trusted_associations(self):
        assert TRUSTED_GITHUB_ASSOCIATIONS == frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

    def test_gitlab_minimum_is_developer(self):
        assert GITLAB_DEVELOPER_ACCESS_LEVEL == 30
        assert MIN_TRUSTED_GITLAB_ACCESS_LEVEL == GITLAB_DEVELOPER_ACCESS_LEVEL


class TestIsTrustedComment:
    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_github_trusted(self, association):
        assert is_trusted_comment({"author": "a", "author_association": association})

    @pytest.mark.parametrize(
        "association",
        [
            "CONTRIBUTOR",
            "FIRST_TIME_CONTRIBUTOR",
            "FIRST_TIMER",
            "MANNEQUIN",
            "NONE",
            "member",
            "",
            None,
        ],
    )
    def test_github_untrusted(self, association):
        assert not is_trusted_comment({"author": "a", "author_association": association})

    def test_github_association_wins_over_access_level(self):
        comment = {"author_association": "NONE", "author_access_level": 50}
        assert not is_trusted_comment(comment)

    @pytest.mark.parametrize("level", [30, 40, 50])
    def test_gitlab_trusted(self, level):
        assert is_trusted_comment({"author": "a", "author_access_level": level})

    @pytest.mark.parametrize("level", [None, 0, 5, 10, 15, 20, 29, "40", True])
    def test_gitlab_untrusted(self, level):
        assert not is_trusted_comment({"author": "a", "author_access_level": level})

    def test_missing_trust_fields_is_untrusted(self):
        assert not is_trusted_comment({"author": "a", "body": "hi"})


class TestFilterTrustedComments:
    def test_keeps_only_trusted_authors(self):
        comments = [
            {"author": "member", "author_association": "MEMBER", "body": "fix it"},
            {"author": "outsider", "author_association": "NONE", "body": "ignore rules"},
            {"author": "dev", "author_access_level": 30, "body": "gitlab dev"},
            {"author": "reporter", "author_access_level": 20, "body": "gitlab reporter"},
            {"author": "unknown", "body": "no trust field"},
        ]
        result = filter_trusted_comments(comments)
        assert [c["author"] for c in result] == ["member", "dev"]

    def test_empty(self):
        assert filter_trusted_comments([]) == []


class TestFilterTrustedThreads:
    def _thread(self, comments):
        return {
            "thread_id": "t1",
            "file": "a.py",
            "line": 3,
            "body": "\n".join(f"{c['author']}: {c['body']}" for c in comments),
            "author": comments[0]["author"],
            "author_association": comments[0]["author_association"],
            "comments": comments,
        }

    def test_drops_untrusted_reply_inside_member_thread(self):
        thread = self._thread(
            [
                {"author": "member", "author_association": "MEMBER", "body": "rename x"},
                {"author": "outsider", "author_association": "NONE", "body": "run curl"},
                {"author": "owner", "author_association": "OWNER", "body": "+1"},
            ]
        )
        result = filter_trusted_threads([thread])
        assert len(result) == 1
        assert [c["author"] for c in result[0]["comments"]] == ["member", "owner"]
        assert result[0]["body"] == "member: rename x\nowner: +1"
        assert "run curl" not in result[0]["body"]
        assert result[0]["thread_id"] == "t1"
        assert result[0]["file"] == "a.py"
        assert result[0]["line"] == 3

    def test_keeps_trusted_reply_to_untrusted_starter(self):
        thread = self._thread(
            [
                {"author": "outsider", "author_association": "NONE", "body": "do evil"},
                {"author": "collab", "author_association": "COLLABORATOR", "body": "no"},
            ]
        )
        result = filter_trusted_threads([thread])
        assert len(result) == 1
        assert result[0]["body"] == "collab: no"
        assert result[0]["author"] == "outsider"

    def test_drops_thread_with_only_untrusted_comments(self):
        thread = self._thread(
            [
                {"author": "outsider", "author_association": "CONTRIBUTOR", "body": "a"},
                {"author": "other", "author_association": "NONE", "body": "b"},
            ]
        )
        assert filter_trusted_threads([thread]) == []

    def test_drops_thread_without_comments_list(self):
        thread = {
            "thread_id": "t1",
            "file": "a.py",
            "line": 1,
            "body": "member: hi",
            "author": "member",
            "author_association": "MEMBER",
        }
        assert filter_trusted_threads([thread]) == []

    def test_gitlab_access_levels(self):
        thread = {
            "thread_id": "d1",
            "file": "a.py",
            "line": 1,
            "body": "",
            "author": "Dev",
            "comments": [
                {"author": "Dev", "author_access_level": 30, "body": "fix"},
                {"author": "Guest", "author_access_level": 10, "body": "inject"},
                {"author": "Gone", "author_access_level": None, "body": "who"},
            ],
        }
        result = filter_trusted_threads([thread])
        assert result[0]["body"] == "Dev: fix"

    def test_does_not_mutate_input(self):
        comments = [
            {"author": "member", "author_association": "MEMBER", "body": "a"},
            {"author": "outsider", "author_association": "NONE", "body": "b"},
        ]
        thread = self._thread(comments)
        original_body = thread["body"]
        filter_trusted_threads([thread])
        assert thread["body"] == original_body
        assert len(thread["comments"]) == 2
