"""Tests for GitLabForge with mocked HTTP."""

from unittest.mock import MagicMock, patch

import pytest

from agentic_ci.forge import ForgeError
from agentic_ci.forge.gitlab import GitLabForge, _find_first_added_line


@pytest.fixture()
def mock_session():
    with patch("agentic_ci.forge.gitlab.build_session") as mock_build:
        session = MagicMock()
        mock_build.return_value = session
        yield session


@pytest.fixture()
def forge(mock_session):
    return GitLabForge()


def _make_response(status_code, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


class TestProjectId:
    def test_returns_numeric_id(self, forge, mock_session):
        mock_session.get.return_value = _make_response(200, {"id": 12345})
        assert forge.project_id("my-org/my-repo") == 12345

    def test_raises_on_http_error(self, forge, mock_session):
        mock_session.get.return_value = _make_response(404, text="Not found")
        with pytest.raises(ForgeError, match="HTTP 404"):
            forge.project_id("nonexistent/project")


class TestCreateMergeRequest:
    def test_success_returns_url(self, forge, mock_session):
        mock_session.get.return_value = _make_response(200, {"id": 1})
        mock_session.post.return_value = _make_response(
            201, {"web_url": "https://gitlab.com/org/repo/-/merge_requests/99"}
        )
        url, error = forge.create_merge_request(
            "https://gitlab.com/org/repo",
            "feature-branch",
            "main",
            "Fix bug",
            "Description",
        )
        assert url == "https://gitlab.com/org/repo/-/merge_requests/99"
        assert error is None

    def test_failure_returns_error(self, forge, mock_session):
        mock_session.get.return_value = _make_response(200, {"id": 1})
        error_resp = _make_response(422, {"message": "Branch already exists"}, "error body")
        mock_session.post.return_value = error_resp
        url, error = forge.create_merge_request(
            "https://gitlab.com/org/repo",
            "feature-branch",
            "main",
            "Fix bug",
            "Description",
        )
        assert url is None
        assert error == "Branch already exists"


class TestUpdateMergeRequest:
    def test_updates_description(self, forge, mock_session):
        mock_session.get.return_value = _make_response(200, {"id": 1})
        mock_session.put.return_value = _make_response(200)
        forge.update_description(
            "https://gitlab.com/org/repo/-/merge_requests/10",
            description="Updated desc",
        )
        mock_session.put.assert_called_once()
        call_kwargs = mock_session.put.call_args
        assert call_kwargs[1]["json"] == {"description": "Updated desc"}

    def test_updates_title(self, forge, mock_session):
        mock_session.get.return_value = _make_response(200, {"id": 1})
        mock_session.put.return_value = _make_response(200)
        forge.update_description(
            "https://gitlab.com/org/repo/-/merge_requests/10",
            title="New title",
        )
        call_kwargs = mock_session.put.call_args
        assert call_kwargs[1]["json"] == {"title": "New title"}

    def test_updates_both(self, forge, mock_session):
        mock_session.get.return_value = _make_response(200, {"id": 1})
        mock_session.put.return_value = _make_response(200)
        forge.update_description(
            "https://gitlab.com/org/repo/-/merge_requests/10",
            title="New title",
            description="New desc",
        )
        call_kwargs = mock_session.put.call_args
        assert call_kwargs[1]["json"] == {"title": "New title", "description": "New desc"}

    def test_noop_when_nothing_provided(self, forge, mock_session):
        forge.update_description("https://gitlab.com/org/repo/-/merge_requests/10")
        mock_session.put.assert_not_called()

    def test_raises_on_failure(self, forge, mock_session):
        mock_session.get.return_value = _make_response(200, {"id": 1})
        mock_session.put.return_value = _make_response(403, {"message": "Forbidden"}, "Forbidden")
        with pytest.raises(ForgeError, match="HTTP 403"):
            forge.update_description(
                "https://gitlab.com/org/repo/-/merge_requests/10",
                description="x",
            )


class TestMrStatus:
    def test_normalizes_opened_to_open(self, forge, mock_session):
        mr_resp = _make_response(200, {"state": "opened", "source_branch": "fix/bug"})
        pipeline_resp = _make_response(200, [{"status": "success"}])
        project_resp = _make_response(200, {"id": 1})
        mock_session.get.side_effect = [project_resp, mr_resp, pipeline_resp]

        result = forge.mr_status("https://gitlab.com/org/repo/-/merge_requests/10")
        assert result["state"] == "open"
        assert result["source_branch"] == "fix/bug"
        assert result["pipeline_status"] == "success"

    def test_merged_state_preserved(self, forge, mock_session):
        mr_resp = _make_response(200, {"state": "merged", "source_branch": "feat"})
        pipeline_resp = _make_response(200, [])
        project_resp = _make_response(200, {"id": 1})
        mock_session.get.side_effect = [project_resp, mr_resp, pipeline_resp]

        result = forge.mr_status("https://gitlab.com/org/repo/-/merge_requests/5")
        assert result["state"] == "merged"
        assert result["pipeline_status"] == "unknown"

    def test_http_error_raises(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        mr_resp = _make_response(404, text="Not found")
        mock_session.get.side_effect = [project_resp, mr_resp]

        with pytest.raises(ForgeError, match="HTTP 404"):
            forge.mr_status("https://gitlab.com/org/repo/-/merge_requests/10")


class TestReviewComments:
    def test_filters_resolved_threads(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        discussions = [
            {
                "id": "d1",
                "individual_note": False,
                "notes": [
                    {
                        "resolved": True,
                        "position": {"new_path": "a.py", "new_line": 10},
                        "body": "resolved comment",
                        "author": {"name": "Alice"},
                    }
                ],
            },
            {
                "id": "d2",
                "individual_note": False,
                "notes": [
                    {
                        "resolved": False,
                        "position": {"new_path": "b.py", "new_line": 20},
                        "body": "unresolved comment",
                        "author": {"name": "Bob"},
                    }
                ],
            },
        ]
        disc_resp = _make_response(200, discussions)
        mock_session.get.side_effect = [project_resp, disc_resp]

        threads = forge.review_comments("https://gitlab.com/org/repo/-/merge_requests/1")
        assert len(threads) == 1
        assert threads[0]["thread_id"] == "d2"
        assert threads[0]["file"] == "b.py"
        assert threads[0]["line"] == 20

    def test_skips_individual_notes(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        discussions = [
            {
                "id": "d1",
                "individual_note": True,
                "notes": [
                    {
                        "resolved": False,
                        "position": {"new_path": "a.py", "new_line": 1},
                        "body": "note",
                        "author": {"name": "Alice"},
                    }
                ],
            },
        ]
        disc_resp = _make_response(200, discussions)
        mock_session.get.side_effect = [project_resp, disc_resp]

        threads = forge.review_comments("https://gitlab.com/org/repo/-/merge_requests/1")
        assert threads == []


class TestGeneralComments:
    def test_filters_system_notes(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        discussions = [
            {
                "notes": [
                    {
                        "system": True,
                        "body": "changed the title",
                        "author": {"name": "System"},
                        "created_at": "2025-01-01T00:00:00Z",
                    }
                ],
            },
            {
                "notes": [
                    {
                        "system": False,
                        "body": "Looks good to me",
                        "author": {"name": "Alice"},
                        "created_at": "2025-01-02T00:00:00Z",
                    }
                ],
            },
        ]
        disc_resp = _make_response(200, discussions)
        mock_session.get.side_effect = [project_resp, disc_resp]

        comments = forge.general_comments("https://gitlab.com/org/repo/-/merge_requests/1")
        assert len(comments) == 1
        assert comments[0]["author"] == "Alice"

    def test_filters_skip_patterns(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        discussions = [
            {
                "notes": [
                    {
                        "body": "<!-- agentic-ci --> automated comment",
                        "author": {"name": "Bot"},
                        "created_at": "2025-01-01T00:00:00Z",
                    }
                ],
            },
            {
                "notes": [
                    {
                        "body": "Real feedback here",
                        "author": {"name": "Alice"},
                        "created_at": "2025-01-02T00:00:00Z",
                    }
                ],
            },
        ]
        disc_resp = _make_response(200, discussions)
        mock_session.get.side_effect = [project_resp, disc_resp]

        comments = forge.general_comments("https://gitlab.com/org/repo/-/merge_requests/1")
        assert len(comments) == 1
        assert comments[0]["body"] == "Real feedback here"

    def test_since_filter(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        discussions = [
            {
                "notes": [
                    {
                        "body": "Old comment",
                        "author": {"name": "Alice"},
                        "created_at": "2025-01-01T00:00:00Z",
                    }
                ],
            },
            {
                "notes": [
                    {
                        "body": "New comment",
                        "author": {"name": "Bob"},
                        "created_at": "2025-06-01T00:00:00Z",
                    }
                ],
            },
        ]
        disc_resp = _make_response(200, discussions)
        mock_session.get.side_effect = [project_resp, disc_resp]

        comments = forge.general_comments(
            "https://gitlab.com/org/repo/-/merge_requests/1",
            since="2025-03-01T00:00:00Z",
        )
        assert len(comments) == 1
        assert comments[0]["body"] == "New comment"


class TestReply:
    def test_posts_note(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        post_resp = _make_response(201)
        mock_session.get.return_value = project_resp
        mock_session.post.return_value = post_resp

        forge.reply(
            "https://gitlab.com/org/repo/-/merge_requests/1",
            "thread-abc",
            "Thanks for the feedback",
        )
        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert call_kwargs[1]["json"]["body"] == "Thanks for the feedback"

    def test_raises_on_error(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        post_resp = _make_response(403, text="Forbidden")
        mock_session.get.return_value = project_resp
        mock_session.post.return_value = post_resp

        with pytest.raises(ForgeError, match="HTTP 403"):
            forge.reply(
                "https://gitlab.com/org/repo/-/merge_requests/1",
                "thread-abc",
                "msg",
            )


class TestResolve:
    def test_resolves_discussion(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        put_resp = _make_response(200)
        mock_session.get.return_value = project_resp
        mock_session.put.return_value = put_resp

        forge.resolve(
            "https://gitlab.com/org/repo/-/merge_requests/1",
            "thread-abc",
        )
        mock_session.put.assert_called_once()
        call_kwargs = mock_session.put.call_args
        assert call_kwargs[1]["json"]["resolved"] is True

    def test_raises_on_error(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        put_resp = _make_response(500, text="Server error")
        mock_session.get.return_value = project_resp
        mock_session.put.return_value = put_resp

        with pytest.raises(ForgeError, match="HTTP 500"):
            forge.resolve(
                "https://gitlab.com/org/repo/-/merge_requests/1",
                "thread-abc",
            )


class TestPipelineFailures:
    def test_returns_failed_jobs_with_logs(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        pipeline_resp = _make_response(200, [{"id": 100, "status": "failed"}])
        jobs_resp = _make_response(
            200, [{"id": 501, "name": "unit-tests"}, {"id": 502, "name": "lint"}]
        )
        trace_resp1 = _make_response(200)
        trace_resp1.text = "line1\nline2\nERROR: test failed"
        trace_resp2 = _make_response(200)
        trace_resp2.text = "lint output\nwarning found"
        mock_session.get.side_effect = [
            project_resp,
            pipeline_resp,
            jobs_resp,
            trace_resp1,
            trace_resp2,
        ]

        result = forge.pipeline_failures("https://gitlab.com/org/repo/-/merge_requests/1")
        assert result["pipeline_status"] == "failed"
        assert len(result["failed_jobs"]) == 2
        assert result["failed_jobs"][0]["name"] == "unit-tests"
        assert "ERROR: test failed" in result["failed_jobs"][0]["log"]

    def test_success_pipeline_returns_empty(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        pipeline_resp = _make_response(200, [{"id": 100, "status": "success"}])
        mock_session.get.side_effect = [project_resp, pipeline_resp]

        result = forge.pipeline_failures("https://gitlab.com/org/repo/-/merge_requests/1")
        assert result["pipeline_status"] == "success"
        assert result["failed_jobs"] == []

    def test_no_pipelines(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        pipeline_resp = _make_response(200, [])
        mock_session.get.side_effect = [project_resp, pipeline_resp]

        result = forge.pipeline_failures("https://gitlab.com/org/repo/-/merge_requests/1")
        assert result["pipeline_status"] == "none"
        assert result["failed_jobs"] == []


class TestMrDiffPosition:
    def test_finds_first_added_line(self, forge, mock_session):
        project_resp = _make_response(200, {"id": 1})
        changes_resp = _make_response(
            200,
            {
                "diff_refs": {
                    "base_sha": "aaa",
                    "head_sha": "bbb",
                    "start_sha": "ccc",
                },
                "changes": [
                    {
                        "new_path": "src/fix.py",
                        "diff": "@@ -1,3 +1,4 @@\n context\n+added line\n more",
                    }
                ],
            },
        )
        mock_session.get.side_effect = [project_resp, changes_resp]

        result = forge.mr_diff_position("https://gitlab.com/org/repo/-/merge_requests/1")
        assert result["file"] == "src/fix.py"
        assert result["line"] == 2
        assert result["base_sha"] == "aaa"
        assert result["head_sha"] == "bbb"
        assert result["start_sha"] == "ccc"


class TestFindFirstAddedLine:
    def test_simple_addition(self):
        diff = "@@ -1,3 +1,4 @@\n context\n+added\n more"
        assert _find_first_added_line(diff) == 2

    def test_addition_at_start(self):
        diff = "@@ -0,0 +1,2 @@\n+first line\n+second line"
        assert _find_first_added_line(diff) == 1

    def test_no_additions(self):
        diff = "@@ -1,3 +1,2 @@\n context\n-removed\n more"
        assert _find_first_added_line(diff) is None

    def test_deletion_before_addition(self):
        diff = "@@ -1,3 +1,3 @@\n context\n-old\n+new\n more"
        assert _find_first_added_line(diff) == 2

    def test_no_newline_metadata_does_not_shift_line(self):
        diff = "@@ -1,2 +1,3 @@\n existing\n\\ No newline at end of file\n+added line"
        assert _find_first_added_line(diff) == 2
