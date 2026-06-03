"""Tests for GitHubForge with mocked HTTP."""

from unittest.mock import MagicMock, patch

import pytest

from agentic_ci.forge import ForgeError
from agentic_ci.forge.github import GitHubForge, _derive_pipeline_status


@pytest.fixture()
def mock_session():
    with patch("agentic_ci.forge.github.build_session") as mock_build:
        session = MagicMock()
        mock_build.return_value = session
        yield session


@pytest.fixture()
def forge(mock_session):
    return GitHubForge(token="test-token")


def _make_response(status_code, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


class TestCreateMergeRequest:
    def test_success_returns_url(self, forge, mock_session):
        mock_session.post.return_value = _make_response(
            201, {"html_url": "https://github.com/owner/repo/pull/42"}
        )
        url, error = forge.create_merge_request(
            "https://github.com/owner/repo",
            "feature-branch",
            "main",
            "Fix bug",
            "Description",
        )
        assert url == "https://github.com/owner/repo/pull/42"
        assert error is None

    def test_failure_returns_error(self, forge, mock_session):
        mock_session.post.return_value = _make_response(
            422, {"message": "Validation Failed"}, "error body"
        )
        url, error = forge.create_merge_request(
            "https://github.com/owner/repo",
            "feature-branch",
            "main",
            "Fix bug",
            "Description",
        )
        assert url is None
        assert error == "Validation Failed"


class TestUpdateMergeRequest:
    def test_updates_body(self, forge, mock_session):
        mock_session.patch.return_value = _make_response(200)
        forge.update_description(
            "https://github.com/owner/repo/pull/42",
            description="Updated desc",
        )
        mock_session.patch.assert_called_once()
        call_kwargs = mock_session.patch.call_args
        assert call_kwargs[1]["json"] == {"body": "Updated desc"}

    def test_updates_title(self, forge, mock_session):
        mock_session.patch.return_value = _make_response(200)
        forge.update_description(
            "https://github.com/owner/repo/pull/42",
            title="New title",
        )
        call_kwargs = mock_session.patch.call_args
        assert call_kwargs[1]["json"] == {"title": "New title"}

    def test_noop_when_nothing_provided(self, forge, mock_session):
        forge.update_description("https://github.com/owner/repo/pull/42")
        mock_session.patch.assert_not_called()

    def test_raises_on_failure(self, forge, mock_session):
        mock_session.patch.return_value = _make_response(403, {"message": "Forbidden"}, "Forbidden")
        with pytest.raises(ForgeError, match="HTTP 403"):
            forge.update_description(
                "https://github.com/owner/repo/pull/42",
                description="x",
            )


class TestMrStatus:
    def test_open_pr(self, forge, mock_session):
        pr_resp = _make_response(
            200,
            {
                "state": "open",
                "merged": False,
                "head": {"sha": "abc123", "ref": "feature"},
            },
        )
        check_runs_resp = _make_response(
            200,
            {
                "check_runs": [
                    {"status": "completed", "conclusion": "success"},
                ]
            },
        )
        mock_session.get.side_effect = [pr_resp, check_runs_resp]

        result = forge.mr_status("https://github.com/owner/repo/pull/10")
        assert result["state"] == "open"
        assert result["source_branch"] == "feature"
        assert result["pipeline_status"] == "success"

    def test_merged_pr(self, forge, mock_session):
        pr_resp = _make_response(
            200,
            {
                "state": "closed",
                "merged": True,
                "head": {"sha": "abc123", "ref": "feature"},
            },
        )
        check_runs_resp = _make_response(200, {"check_runs": []})
        mock_session.get.side_effect = [pr_resp, check_runs_resp]

        result = forge.mr_status("https://github.com/owner/repo/pull/10")
        assert result["state"] == "merged"

    def test_http_error_raises(self, forge, mock_session):
        mock_session.get.return_value = _make_response(404, text="Not found")
        with pytest.raises(ForgeError, match="HTTP 404"):
            forge.mr_status("https://github.com/owner/repo/pull/10")


class TestReviewComments:
    def test_returns_unresolved_threads(self, forge, mock_session):
        graphql_data = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "thread-1",
                                    "isResolved": False,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "body": "Fix this",
                                                "path": "src/main.py",
                                                "line": 42,
                                                "author": {"login": "reviewer"},
                                            }
                                        ]
                                    },
                                },
                                {
                                    "id": "thread-2",
                                    "isResolved": True,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "body": "Already fixed",
                                                "path": "src/old.py",
                                                "line": 1,
                                                "author": {"login": "someone"},
                                            }
                                        ]
                                    },
                                },
                            ]
                        }
                    }
                }
            }
        }
        graphql_resp = _make_response(200, graphql_data)
        mock_session.post.return_value = graphql_resp

        threads = forge.review_comments("https://github.com/owner/repo/pull/5")
        assert len(threads) == 1
        assert threads[0]["thread_id"] == "thread-1"
        assert threads[0]["file"] == "src/main.py"
        assert threads[0]["line"] == 42
        assert threads[0]["author"] == "reviewer"


class TestGeneralComments:
    def test_filters_skip_patterns(self, forge, mock_session):
        page1 = _make_response(
            200,
            [
                {
                    "body": "<!-- agentic-ci --> automated",
                    "user": {"login": "bot"},
                    "created_at": "2025-01-01T00:00:00Z",
                },
                {
                    "body": "Real feedback",
                    "user": {"login": "human"},
                    "created_at": "2025-01-02T00:00:00Z",
                },
            ],
        )
        mock_session.get.return_value = page1

        comments = forge.general_comments("https://github.com/owner/repo/pull/5")
        assert len(comments) == 1
        assert comments[0]["body"] == "Real feedback"
        assert comments[0]["author"] == "human"

    def test_since_parameter_passed(self, forge, mock_session):
        mock_session.get.return_value = _make_response(200, [])

        forge.general_comments(
            "https://github.com/owner/repo/pull/5",
            since="2025-06-01T00:00:00Z",
        )
        call_kwargs = mock_session.get.call_args
        assert call_kwargs[1]["params"]["since"] == "2025-06-01T00:00:00Z"

    def test_custom_skip_patterns(self, forge, mock_session):
        page1 = _make_response(
            200,
            [
                {
                    "body": "SKIP_ME: this comment",
                    "user": {"login": "bot"},
                    "created_at": "2025-01-01T00:00:00Z",
                },
                {
                    "body": "Keep this one",
                    "user": {"login": "human"},
                    "created_at": "2025-01-02T00:00:00Z",
                },
            ],
        )
        mock_session.get.return_value = page1

        comments = forge.general_comments(
            "https://github.com/owner/repo/pull/5",
            skip_patterns=["SKIP_ME"],
        )
        assert len(comments) == 1
        assert comments[0]["body"] == "Keep this one"


class TestReply:
    def test_calls_graphql_mutation(self, forge, mock_session):
        mock_session.post.return_value = _make_response(
            200,
            {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "c1"}}}},
        )
        forge.reply(
            "https://github.com/owner/repo/pull/5",
            "thread-1",
            "Thanks for the review",
        )
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        payload = call_args[1]["json"]
        assert "mutation" in payload["query"]
        assert payload["variables"]["threadId"] == "thread-1"
        assert payload["variables"]["body"] == "Thanks for the review"


class TestResolve:
    def test_calls_graphql_mutation(self, forge, mock_session):
        mock_session.post.return_value = _make_response(
            200,
            {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}},
        )
        forge.resolve(
            "https://github.com/owner/repo/pull/5",
            "thread-1",
        )
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        payload = call_args[1]["json"]
        assert "mutation" in payload["query"]
        assert payload["variables"]["threadId"] == "thread-1"


class TestPipelineFailures:
    def test_returns_failed_jobs(self, forge, mock_session):
        pr_resp = _make_response(200, {"head": {"sha": "abc123"}})
        check_runs_resp = _make_response(
            200,
            {
                "check_runs": [
                    {
                        "id": 101,
                        "name": "unit-tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "output": {},
                    },
                    {
                        "id": 102,
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ]
            },
        )
        log_resp = _make_response(200)
        log_resp.text = "ERROR: test_foo failed"
        mock_session.get.side_effect = [pr_resp, check_runs_resp, log_resp]

        result = forge.pipeline_failures("https://github.com/owner/repo/pull/5")
        assert result["pipeline_status"] == "failed"
        assert len(result["failed_jobs"]) == 1
        assert result["failed_jobs"][0]["name"] == "unit-tests"
        assert "test_foo failed" in result["failed_jobs"][0]["log"]

    def test_no_head_sha(self, forge, mock_session):
        pr_resp = _make_response(200, {"head": {}})
        mock_session.get.return_value = pr_resp

        result = forge.pipeline_failures("https://github.com/owner/repo/pull/5")
        assert result["pipeline_status"] == "none"
        assert result["failed_jobs"] == []

    def test_success_pipeline(self, forge, mock_session):
        pr_resp = _make_response(200, {"head": {"sha": "abc123"}})
        check_runs_resp = _make_response(
            200,
            {
                "check_runs": [
                    {"status": "completed", "conclusion": "success"},
                ]
            },
        )
        mock_session.get.side_effect = [pr_resp, check_runs_resp]

        result = forge.pipeline_failures("https://github.com/owner/repo/pull/5")
        assert result["pipeline_status"] == "success"
        assert result["failed_jobs"] == []


class TestCheckRuns:
    def test_handles_403(self, forge, mock_session):
        mock_session.get.return_value = _make_response(403, text="Forbidden")
        runs, accessible = forge.check_runs("owner/repo", "sha123")
        assert runs == []
        assert accessible is False

    def test_returns_runs(self, forge, mock_session):
        mock_session.get.return_value = _make_response(
            200,
            {
                "check_runs": [
                    {"id": 1, "name": "test", "status": "completed", "conclusion": "success"},
                ]
            },
        )
        runs, accessible = forge.check_runs("owner/repo", "sha123")
        assert len(runs) == 1
        assert accessible is True

    def test_http_error_raises(self, forge, mock_session):
        mock_session.get.return_value = _make_response(500, text="Server error")
        with pytest.raises(ForgeError, match="HTTP 500"):
            forge.check_runs("owner/repo", "sha123")


class TestDerivePipelineStatus:
    def test_not_accessible(self):
        assert _derive_pipeline_status([], accessible=False) == "unknown"

    def test_no_check_runs(self):
        assert _derive_pipeline_status([]) == "none"

    def test_running(self):
        runs = [{"status": "in_progress", "conclusion": None}]
        assert _derive_pipeline_status(runs) == "running"

    def test_all_success(self):
        runs = [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "success"},
        ]
        assert _derive_pipeline_status(runs) == "success"

    def test_one_failure(self):
        runs = [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "failure"},
        ]
        assert _derive_pipeline_status(runs) == "failed"

    def test_timed_out(self):
        runs = [{"status": "completed", "conclusion": "timed_out"}]
        assert _derive_pipeline_status(runs) == "failed"

    def test_startup_failure(self):
        runs = [{"status": "completed", "conclusion": "startup_failure"}]
        assert _derive_pipeline_status(runs) == "failed"

    def test_cancelled(self):
        runs = [{"status": "completed", "conclusion": "cancelled"}]
        assert _derive_pipeline_status(runs) == "failed"

    def test_action_required(self):
        runs = [{"status": "completed", "conclusion": "action_required"}]
        assert _derive_pipeline_status(runs) == "failed"

    def test_neutral_is_success(self):
        runs = [{"status": "completed", "conclusion": "neutral"}]
        assert _derive_pipeline_status(runs) == "success"

    def test_unknown_conclusion(self):
        runs = [{"status": "completed", "conclusion": "some_new_state"}]
        assert _derive_pipeline_status(runs) == "unknown"


class TestFindPrivateKey:
    def test_finds_in_secure_dir(self, tmp_path, monkeypatch):
        secure_dir = tmp_path / "secure"
        secure_dir.mkdir()
        key_file = secure_dir / "app.pem"
        key_file.write_text("KEY")
        monkeypatch.setenv("SECURE_FILES_DOWNLOAD_PATH", str(secure_dir))

        from agentic_ci.forge.github import _find_private_key

        result = _find_private_key("app.pem")
        assert result == key_file

    def test_finds_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SECURE_FILES_DOWNLOAD_PATH", raising=False)
        key_file = tmp_path / "app.pem"
        key_file.write_text("KEY")

        from agentic_ci.forge.github import _find_private_key

        result = _find_private_key("app.pem")
        assert result is not None
        assert result.resolve() == key_file.resolve()

    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SECURE_FILES_DOWNLOAD_PATH", str(tmp_path / "nonexistent"))

        from agentic_ci.forge.github import _find_private_key

        result = _find_private_key("missing.pem")
        assert result is None

    def test_secure_dir_takes_precedence(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        secure_dir = tmp_path / "secure"
        secure_dir.mkdir()
        secure_key = secure_dir / "app.pem"
        secure_key.write_text("SECURE_KEY")
        cwd_key = tmp_path / "app.pem"
        cwd_key.write_text("CWD_KEY")
        monkeypatch.setenv("SECURE_FILES_DOWNLOAD_PATH", str(secure_dir))

        from agentic_ci.forge.github import _find_private_key

        result = _find_private_key("app.pem")
        assert result == secure_key

    def test_default_secure_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SECURE_FILES_DOWNLOAD_PATH", raising=False)
        secure_dir = tmp_path / ".secure_files"
        secure_dir.mkdir()
        key_file = secure_dir / "app.pem"
        key_file.write_text("KEY")

        from agentic_ci.forge.github import _find_private_key

        result = _find_private_key("app.pem")
        assert result is not None
        assert result.resolve() == key_file.resolve()


class TestResolveAppToken:
    """Tests for resolve_app_token()."""

    @pytest.fixture()
    def github_config(self):
        return {
            "opendatahub-io": {
                "credentials_env": "GITHUB_APP_ODH",
                "private_key_file": "odh-app.pem",
            }
        }

    @pytest.fixture()
    def valid_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "GITHUB_APP_ODH",
            '{"app_id": "12345", "installation_id": "67890"}',
        )
        key_file = tmp_path / "odh-app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----")
        monkeypatch.setenv("SECURE_FILES_DOWNLOAD_PATH", str(tmp_path))
        return tmp_path

    def test_valid_flow(self, github_config, valid_env):
        from agentic_ci.forge.github import resolve_app_token

        with (
            patch("agentic_ci.forge.github.generate_github_jwt", return_value="jwt-token"),
            patch(
                "agentic_ci.forge.github.get_installation_token",
                return_value="install-token",
            ),
        ):
            token = resolve_app_token("https://github.com/opendatahub-io/repo", github_config)
        assert token == "install-token"

    def test_missing_org_in_url(self, github_config):
        from agentic_ci.forge.github import resolve_app_token

        result = resolve_app_token("https://example.com/repo", github_config)
        assert result is None

    def test_no_config_for_org(self, github_config):
        from agentic_ci.forge.github import resolve_app_token

        result = resolve_app_token("https://github.com/unknown-org/repo", github_config)
        assert result is None

    def test_missing_env_var(self, github_config, monkeypatch):
        from agentic_ci.forge.github import resolve_app_token

        monkeypatch.delenv("GITHUB_APP_ODH", raising=False)
        result = resolve_app_token("https://github.com/opendatahub-io/repo", github_config)
        assert result is None

    def test_invalid_json_env(self, github_config, monkeypatch):
        from agentic_ci.forge.github import resolve_app_token

        monkeypatch.setenv("GITHUB_APP_ODH", "not-json")
        result = resolve_app_token("https://github.com/opendatahub-io/repo", github_config)
        assert result is None

    def test_missing_pem_file(self, github_config, monkeypatch, tmp_path):
        from agentic_ci.forge.github import resolve_app_token

        monkeypatch.setenv(
            "GITHUB_APP_ODH",
            '{"app_id": "12345", "installation_id": "67890"}',
        )
        monkeypatch.setenv("SECURE_FILES_DOWNLOAD_PATH", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        result = resolve_app_token("https://github.com/opendatahub-io/repo", github_config)
        assert result is None

    def test_case_insensitive_org_match(self, github_config, valid_env):
        from agentic_ci.forge.github import resolve_app_token

        with (
            patch("agentic_ci.forge.github.generate_github_jwt", return_value="jwt"),
            patch("agentic_ci.forge.github.get_installation_token", return_value="token"),
        ):
            token = resolve_app_token("https://github.com/OpenDataHub-IO/repo", github_config)
        assert token == "token"

    def test_incomplete_config_no_credentials_env(self):
        from agentic_ci.forge.github import resolve_app_token

        config = {"myorg": {"private_key_file": "key.pem"}}
        result = resolve_app_token("https://github.com/myorg/repo", config)
        assert result is None

    def test_incomplete_config_no_key_file(self, monkeypatch):
        from agentic_ci.forge.github import resolve_app_token

        monkeypatch.setenv("MY_ENV", '{"app_id": "1", "installation_id": "2"}')
        config = {"myorg": {"credentials_env": "MY_ENV"}}
        result = resolve_app_token("https://github.com/myorg/repo", config)
        assert result is None

    def test_missing_app_id(self, github_config, monkeypatch, tmp_path):
        from agentic_ci.forge.github import resolve_app_token

        monkeypatch.setenv("GITHUB_APP_ODH", '{"installation_id": "67890"}')
        monkeypatch.setenv("SECURE_FILES_DOWNLOAD_PATH", str(tmp_path))
        key_file = tmp_path / "odh-app.pem"
        key_file.write_text("KEY")
        result = resolve_app_token("https://github.com/opendatahub-io/repo", github_config)
        assert result is None

    def test_jwt_generation_failure(self, github_config, valid_env):
        from agentic_ci.forge.github import resolve_app_token

        with patch(
            "agentic_ci.forge.github.generate_github_jwt",
            side_effect=Exception("JWT fail"),
        ):
            result = resolve_app_token("https://github.com/opendatahub-io/repo", github_config)
        assert result is None
