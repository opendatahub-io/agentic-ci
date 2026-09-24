"""Tests for forge CLI argument parsing and dispatch."""

import json
from unittest.mock import MagicMock, patch

import pytest

from agentic_ci.forge.cli import register_subcommands
from agentic_ci.forge.github import GitHubForge


def _run_forge_cli(argv: list[str]) -> None:
    """Simulate ``agentic-ci forge <args>`` by building a parser with
    the forge subcommands registered and parsing the given argv."""
    import argparse

    parser = argparse.ArgumentParser()
    register_subcommands(parser)
    args = parser.parse_args(argv)
    args.func(args)


class TestMrStatusCommand:
    def test_dispatches_correctly(self, capsys):
        mock_forge = MagicMock()
        mock_forge.mr_status.return_value = {
            "state": "open",
            "source_branch": "fix/bug",
            "pipeline_status": "success",
        }
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(["mr-status", "https://gitlab.com/o/r/-/merge_requests/1"])

        mock_forge.mr_status.assert_called_once_with("https://gitlab.com/o/r/-/merge_requests/1")
        output = capsys.readouterr().out
        result = json.loads(output)
        assert result["state"] == "open"


class TestMrCommentsCommand:
    def test_dispatches_correctly(self, capsys):
        mock_forge = MagicMock()
        mock_forge.review_comments.return_value = [
            {"thread_id": "t1", "file": "a.py", "line": 10, "body": "fix", "author": "X"}
        ]
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(["mr-comments", "https://gitlab.com/o/r/-/merge_requests/1"])

        mock_forge.review_comments.assert_called_once()


class TestCommentTrustFieldsInOutput:
    """The JSON output carries per-comment trust fields next to the old keys."""

    def _github_forge(self, session):
        with patch("agentic_ci.forge.github.build_session", return_value=session):
            return GitHubForge(token="tok")

    def test_mr_comments_includes_thread_comments(self, capsys):
        session = MagicMock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "t1",
                                    "isResolved": False,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "body": "fix",
                                                "path": "a.py",
                                                "line": 3,
                                                "createdAt": "2026-09-01T00:00:00Z",
                                                "authorAssociation": "MEMBER",
                                                "author": {"login": "m"},
                                            },
                                            {
                                                "body": "evil",
                                                "path": "a.py",
                                                "line": 3,
                                                "createdAt": "2026-09-02T00:00:00Z",
                                                "authorAssociation": "NONE",
                                                "author": {"login": "o"},
                                            },
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        }
        forge = self._github_forge(session)
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=forge):
            _run_forge_cli(["mr-comments", "https://github.com/o/r/pull/1"])

        result = json.loads(capsys.readouterr().out)
        thread = result[0]
        assert thread["thread_id"] == "t1"
        assert thread["file"] == "a.py"
        assert thread["line"] == 3
        assert thread["body"] == "m: fix\no: evil"
        assert thread["author"] == "m"
        assert thread["author_association"] == "MEMBER"
        assert [(c["author"], c["author_association"]) for c in thread["comments"]] == [
            ("m", "MEMBER"),
            ("o", "NONE"),
        ]

    def test_mr_general_comments_includes_association(self, capsys):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = [
            {
                "body": "hi",
                "user": {"login": "c"},
                "author_association": "CONTRIBUTOR",
                "created_at": "2026-09-01T00:00:00Z",
            }
        ]
        forge = self._github_forge(session)
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=forge):
            _run_forge_cli(["mr-general-comments", "https://github.com/o/r/pull/1"])

        result = json.loads(capsys.readouterr().out)
        assert result == [
            {
                "author": "c",
                "author_association": "CONTRIBUTOR",
                "body": "hi",
                "created_at": "2026-09-01T00:00:00Z",
            }
        ]


class TestMrGeneralCommentsCommand:
    def test_with_since(self, capsys):
        mock_forge = MagicMock()
        mock_forge.general_comments.return_value = []
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "mr-general-comments",
                    "https://gitlab.com/o/r/-/merge_requests/1",
                    "--since",
                    "2025-01-01T00:00:00Z",
                ]
            )

        mock_forge.general_comments.assert_called_once_with(
            "https://gitlab.com/o/r/-/merge_requests/1",
            since="2025-01-01T00:00:00Z",
        )


class TestMrReplyCommand:
    def test_dispatches_correctly(self, capsys):
        mock_forge = MagicMock()
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "mr-reply",
                    "https://gitlab.com/o/r/-/merge_requests/1",
                    "thread-abc",
                    "Done",
                ]
            )

        mock_forge.reply.assert_called_once_with(
            "https://gitlab.com/o/r/-/merge_requests/1",
            "thread-abc",
            "Done",
        )


class TestMrResolveCommand:
    def test_dispatches_correctly(self, capsys):
        mock_forge = MagicMock()
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "mr-resolve",
                    "https://gitlab.com/o/r/-/merge_requests/1",
                    "thread-abc",
                ]
            )

        mock_forge.resolve.assert_called_once_with(
            "https://gitlab.com/o/r/-/merge_requests/1",
            "thread-abc",
        )


class TestPipelineFailuresCommand:
    def test_dispatches_correctly(self, capsys):
        mock_forge = MagicMock()
        mock_forge.pipeline_failures.return_value = {
            "pipeline_status": "success",
            "failed_jobs": [],
        }
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "pipeline-failures",
                    "https://gitlab.com/o/r/-/merge_requests/1",
                ]
            )

        mock_forge.pipeline_failures.assert_called_once()


class TestGithubTokenCommand:
    def test_requires_all_args(self):
        with pytest.raises(SystemExit):
            _run_forge_cli(["github-token", "--app-id", "123"])

    def test_dispatches_with_all_args(self, capsys):
        with (
            patch("agentic_ci.forge.cli.generate_github_jwt", return_value="jwt-tok") as mock_jwt,
            patch(
                "agentic_ci.forge.cli.get_installation_token", return_value="inst-tok"
            ) as mock_inst,
        ):
            _run_forge_cli(
                [
                    "github-token",
                    "--app-id",
                    "123",
                    "--installation-id",
                    "456",
                    "--private-key",
                    "PEM-DATA",
                ]
            )

        mock_jwt.assert_called_once_with("123", "PEM-DATA")
        mock_inst.assert_called_once_with("jwt-tok", "456")
        output = capsys.readouterr().out.strip()
        assert output == "inst-tok"

    def test_reads_private_key_from_file(self, tmp_path, capsys):
        pem_file = tmp_path / "key.pem"
        pem_file.write_text("-----BEGIN RSA PRIVATE KEY-----\ndata\n-----END RSA PRIVATE KEY-----")
        with (
            patch("agentic_ci.forge.cli.generate_github_jwt", return_value="jwt-tok"),
            patch("agentic_ci.forge.cli.get_installation_token", return_value="inst-tok"),
        ):
            _run_forge_cli(
                [
                    "github-token",
                    "--app-id",
                    "123",
                    "--installation-id",
                    "456",
                    "--private-key",
                    str(pem_file),
                ]
            )

        output = capsys.readouterr().out.strip()
        assert output == "inst-tok"


class TestLabelExistsCommand:
    def test_dispatches_correctly(self, capsys):
        mock_forge = MagicMock()
        mock_forge.label_exists.return_value = True
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(["label-exists", "https://gitlab.com/o/r", "autofix"])

        mock_forge.label_exists.assert_called_once_with("https://gitlab.com/o/r", "autofix")
        assert json.loads(capsys.readouterr().out) is True


class TestLabelCreateCommand:
    def test_dispatches_with_optional_fields(self, capsys):
        mock_forge = MagicMock()
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "label-create",
                    "https://github.com/o/r",
                    "autofix",
                    "--color",
                    "#FFAABB",
                    "--description",
                    "Opened by autofix",
                ]
            )

        mock_forge.create_label.assert_called_once_with(
            "https://github.com/o/r",
            "autofix",
            color="#FFAABB",
            description="Opened by autofix",
        )


class TestMrAddLabelsCommand:
    def test_dispatches_correctly(self, capsys):
        mock_forge = MagicMock()
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "mr-add-labels",
                    "https://gitlab.com/o/r/-/merge_requests/1",
                    "--labels",
                    "autofix",
                    "package-onboarding",
                ]
            )

        mock_forge.add_mr_labels.assert_called_once_with(
            "https://gitlab.com/o/r/-/merge_requests/1",
            ["autofix", "package-onboarding"],
        )


class TestMrUpdateCommand:
    def test_dispatches_with_description(self, capsys):
        mock_forge = MagicMock()
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "mr-update",
                    "https://gitlab.com/o/r/-/merge_requests/1",
                    "--description",
                    "New desc",
                ]
            )

        mock_forge.update_description.assert_called_once_with(
            "https://gitlab.com/o/r/-/merge_requests/1",
            title=None,
            description="New desc",
        )

    def test_dispatches_with_title(self, capsys):
        mock_forge = MagicMock()
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "mr-update",
                    "https://gitlab.com/o/r/-/merge_requests/1",
                    "--title",
                    "New title",
                ]
            )

        mock_forge.update_description.assert_called_once_with(
            "https://gitlab.com/o/r/-/merge_requests/1",
            title="New title",
            description=None,
        )


class TestMrDiffPositionCommand:
    def test_non_gitlab_exits(self, capsys):
        mock_forge = MagicMock(spec=[])
        with (
            patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge),
            pytest.raises(SystemExit),
        ):
            _run_forge_cli(
                [
                    "mr-diff-position",
                    "https://github.com/o/r/pull/1",
                ]
            )

    def test_gitlab_dispatches(self, capsys):
        from agentic_ci.forge.gitlab import GitLabForge

        mock_forge = MagicMock(spec=GitLabForge)
        mock_forge.mr_diff_position.return_value = {
            "file": "a.py",
            "line": 5,
            "base_sha": "aaa",
            "head_sha": "bbb",
            "start_sha": "ccc",
        }
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge):
            _run_forge_cli(
                [
                    "mr-diff-position",
                    "https://gitlab.com/o/r/-/merge_requests/1",
                ]
            )

        mock_forge.mr_diff_position.assert_called_once()


class TestTokenPassthrough:
    def test_token_passed_to_detect(self):
        mock_forge = MagicMock()
        mock_forge.mr_status.return_value = {
            "state": "open",
            "source_branch": "b",
            "pipeline_status": "unknown",
        }
        with patch("agentic_ci.forge.cli.Forge.detect", return_value=mock_forge) as mock_detect:
            _run_forge_cli(
                [
                    "--token",
                    "my-gh-token",
                    "mr-status",
                    "https://github.com/o/r/pull/1",
                ]
            )

        mock_detect.assert_called_once_with(
            "https://github.com/o/r/pull/1", github_token="my-gh-token"
        )


class TestNoSubcommand:
    def test_exits_without_subcommand(self):
        with pytest.raises(SystemExit):
            _run_forge_cli([])
