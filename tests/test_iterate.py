"""Tests for agentic_ci.iterate module."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from agentic_ci.iterate import (
    already_notified_ready,
    check_mr_state,
    collect_feedback,
    count_iterations,
    extract_mr_url,
    resolve_threads,
)


class TestExtractMrUrl:
    def test_gitlab_url(self):
        comments = [
            {
                "body": (
                    "### agentic-ci bot\n\n"
                    "MR created: https://gitlab.com/org/repo/-/merge_requests/42"
                ),
            },
        ]
        assert extract_mr_url(comments) == ("https://gitlab.com/org/repo/-/merge_requests/42")

    def test_github_url(self):
        comments = [
            {
                "body": ("### agentic-ci bot\n\nPR created: https://github.com/owner/repo/pull/99"),
            },
        ]
        assert extract_mr_url(comments) == "https://github.com/owner/repo/pull/99"

    def test_no_match(self):
        comments = [{"body": "just a regular comment"}]
        assert extract_mr_url(comments) is None

    def test_empty_comments(self):
        assert extract_mr_url([]) is None

    def test_custom_pattern(self):
        custom = re.compile(r"my-bot", re.MULTILINE)
        comments = [
            {
                "body": ("my-bot: https://gitlab.com/a/b/-/merge_requests/1"),
            },
        ]
        assert extract_mr_url(comments, bot_pattern=custom) == (
            "https://gitlab.com/a/b/-/merge_requests/1"
        )

    def test_jira_wiki_header(self):
        comments = [
            {
                "body": ("h3. agentic-ci bot\n\nhttps://gitlab.com/x/y/-/merge_requests/7"),
            },
        ]
        assert extract_mr_url(comments) == ("https://gitlab.com/x/y/-/merge_requests/7")

    def test_gitlab_preferred_over_github(self):
        """When both URLs appear in the same comment, GitLab is matched first."""
        comments = [
            {
                "body": (
                    "### agentic-ci bot\n\n"
                    "https://gitlab.com/org/repo/-/merge_requests/1 "
                    "https://github.com/owner/repo/pull/2"
                ),
            },
        ]
        assert extract_mr_url(comments) == ("https://gitlab.com/org/repo/-/merge_requests/1")


class TestCountIterations:
    def test_zero_iterations(self):
        comments = [{"body": "### agentic-ci bot\n\nMR created."}]
        assert count_iterations(comments) == 0

    def test_one_iteration(self):
        comments = [
            {
                "body": ("### agentic-ci bot\n\nIteration 1/36: addressed 2 review comment(s)."),
            },
        ]
        assert count_iterations(comments) == 1

    def test_multiple_iterations(self):
        comments = [
            {"body": "### agentic-ci bot\n\nIteration 1/36: changes."},
            {"body": "### agentic-ci bot\n\nIteration 2/36: more changes."},
            {"body": "### agentic-ci bot\n\nIteration 3/36: final changes."},
        ]
        assert count_iterations(comments) == 3

    def test_non_bot_comments_ignored(self):
        comments = [
            {"body": "Iteration 1/36: this is from a human"},
            {"body": "### agentic-ci bot\n\nIteration 1/36: real one."},
        ]
        assert count_iterations(comments) == 1


class TestAlreadyNotifiedReady:
    def test_ready_for_review_and_merge(self):
        comments = [
            {
                "body": (
                    "### agentic-ci bot\n\n"
                    "CI is green -- the merge/pull request is "
                    "ready for a maintainer to review and merge."
                ),
            },
        ]
        assert already_notified_ready(comments) is True

    def test_ready_for_merge(self):
        comments = [
            {
                "body": ("### agentic-ci bot\n\nThe MR is ready for a maintainer to merge."),
            },
        ]
        assert already_notified_ready(comments) is True

    def test_not_ready(self):
        comments = [
            {"body": "### agentic-ci bot\n\nIteration 1/36: pushed changes."},
        ]
        assert already_notified_ready(comments) is False

    def test_empty_comments(self):
        assert already_notified_ready([]) is False

    def test_last_comment_wins(self):
        """Only the last bot comment matters."""
        comments = [
            {
                "body": ("### agentic-ci bot\n\nready for a maintainer to review and merge."),
            },
            {
                "body": "### agentic-ci bot\n\nIteration 2/36: pushed changes.",
            },
        ]
        assert already_notified_ready(comments) is False


class TestResolveThreads:
    def test_empty_comments_returns_zero(self):
        assert resolve_threads("https://gitlab.com/o/r/-/merge_requests/1", []) == 0

    @patch("agentic_ci.iterate.detect_forge")
    def test_resolves_threads(self, mock_detect):
        mock_forge = MagicMock()
        mock_detect.return_value = mock_forge

        comments = [
            {"thread_id": "t1", "body": "fix this"},
            {"thread_id": "t2", "body": "and this"},
        ]
        result = resolve_threads(
            "https://gitlab.com/org/repo/-/merge_requests/5",
            comments,
        )
        assert result == 2
        assert mock_forge.reply.call_count == 2
        assert mock_forge.resolve.call_count == 2

    @patch("agentic_ci.iterate.detect_forge")
    def test_skips_comments_without_thread_id(self, mock_detect):
        mock_forge = MagicMock()
        mock_detect.return_value = mock_forge

        comments = [
            {"body": "no thread id"},
            {"thread_id": "t1", "body": "has thread id"},
        ]
        result = resolve_threads(
            "https://gitlab.com/org/repo/-/merge_requests/5",
            comments,
        )
        assert result == 1

    @patch("agentic_ci.iterate.detect_forge")
    def test_forge_detection_failure_returns_zero(self, mock_detect):
        from agentic_ci.forge import ForgeError

        mock_detect.side_effect = ForgeError("bad url")

        comments = [{"thread_id": "t1", "body": "fix"}]
        result = resolve_threads("https://bad.example.com/mr/1", comments)
        assert result == 0

    @patch("agentic_ci.iterate.detect_forge")
    def test_partial_failure(self, mock_detect):
        """When one thread fails to resolve, the others still count."""
        from agentic_ci.forge import ForgeError

        mock_forge = MagicMock()
        mock_forge.reply.side_effect = [None, ForgeError("fail")]
        mock_forge.resolve.return_value = None
        mock_detect.return_value = mock_forge

        comments = [
            {"thread_id": "t1", "body": "first"},
            {"thread_id": "t2", "body": "second"},
        ]
        result = resolve_threads(
            "https://gitlab.com/org/repo/-/merge_requests/5",
            comments,
        )
        assert result == 1

    @patch("agentic_ci.iterate.detect_forge")
    def test_custom_reply_message(self, mock_detect):
        mock_forge = MagicMock()
        mock_detect.return_value = mock_forge

        comments = [{"thread_id": "t1", "body": "fix this"}]
        resolve_threads(
            "https://gitlab.com/o/r/-/merge_requests/1",
            comments,
            reply_message="Done.",
        )
        mock_forge.reply.assert_called_once_with(
            "https://gitlab.com/o/r/-/merge_requests/1",
            "t1",
            "Done.",
        )


class TestCheckMrState:
    @patch("agentic_ci.iterate.detect_forge")
    def test_returns_status(self, mock_detect):
        mock_forge = MagicMock()
        mock_forge.mr_status.return_value = {
            "state": "open",
            "source_branch": "fix/bug",
            "pipeline_status": "success",
        }
        mock_detect.return_value = mock_forge

        result = check_mr_state("https://gitlab.com/o/r/-/merge_requests/1")
        assert result == {
            "state": "open",
            "source_branch": "fix/bug",
            "pipeline_status": "success",
        }

    @patch("agentic_ci.iterate.detect_forge")
    def test_returns_none_on_error(self, mock_detect):
        from agentic_ci.forge import ForgeError

        mock_detect.side_effect = ForgeError("connection failed")

        result = check_mr_state("https://gitlab.com/o/r/-/merge_requests/1")
        assert result is None

    @patch("agentic_ci.iterate.detect_forge")
    def test_passes_github_token(self, mock_detect):
        mock_forge = MagicMock()
        mock_forge.mr_status.return_value = {"state": "open"}
        mock_detect.return_value = mock_forge

        check_mr_state(
            "https://github.com/owner/repo/pull/1",
            github_token="ghp_test",
        )
        mock_detect.assert_called_once_with(
            "https://github.com/owner/repo/pull/1",
            github_token="ghp_test",
        )


class TestCollectFeedback:
    @patch("agentic_ci.iterate.detect_forge")
    def test_collects_all_feedback(self, mock_detect):
        mock_forge = MagicMock()
        mock_forge.review_comments.return_value = [
            {"thread_id": "t1", "body": "fix"},
        ]
        mock_forge.general_comments.return_value = [
            {"body": "looks good"},
        ]
        mock_forge.pipeline_failures.return_value = {
            "pipeline_status": "failed",
            "failed_jobs": [{"name": "lint", "id": "1", "log": "error"}],
        }
        mock_detect.return_value = mock_forge

        result = collect_feedback("https://gitlab.com/o/r/-/merge_requests/1")

        assert result["review_count"] == 1
        assert result["general_count"] == 1
        assert result["ci_fail_count"] == 1
        assert len(result["review_comments"]) == 1
        assert len(result["general_comments"]) == 1

    @patch("agentic_ci.iterate.detect_forge")
    def test_forge_detection_failure_returns_empty(self, mock_detect):
        from agentic_ci.forge import ForgeError

        mock_detect.side_effect = ForgeError("bad url")

        result = collect_feedback("https://bad.example.com/mr/1")
        assert result["review_comments"] == []
        assert result["general_comments"] == []
        assert result["ci_failures"] == {}
        assert result["review_count"] == 0
        assert result["general_count"] == 0
        assert result["ci_fail_count"] == 0

    @patch("agentic_ci.iterate.detect_forge")
    def test_partial_api_failure(self, mock_detect):
        """Individual API call failures produce empty results for that call."""
        from agentic_ci.forge import ForgeError

        mock_forge = MagicMock()
        mock_forge.review_comments.side_effect = ForgeError("api error")
        mock_forge.general_comments.return_value = [{"body": "ok"}]
        mock_forge.pipeline_failures.return_value = {
            "pipeline_status": "success",
            "failed_jobs": [],
        }
        mock_detect.return_value = mock_forge

        result = collect_feedback("https://gitlab.com/o/r/-/merge_requests/1")
        assert result["review_comments"] == []
        assert result["general_count"] == 1
        assert result["ci_fail_count"] == 0

    @patch("agentic_ci.iterate.detect_forge")
    def test_none_returns_from_forge(self, mock_detect):
        """Forge methods returning None are handled gracefully."""
        mock_forge = MagicMock()
        mock_forge.review_comments.return_value = None
        mock_forge.general_comments.return_value = None
        mock_forge.pipeline_failures.return_value = None
        mock_detect.return_value = mock_forge

        result = collect_feedback("https://gitlab.com/o/r/-/merge_requests/1")
        assert result["review_comments"] == []
        assert result["general_comments"] == []
        assert result["ci_failures"] == {}
        assert result["review_count"] == 0
        assert result["ci_fail_count"] == 0
