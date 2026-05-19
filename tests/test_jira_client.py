"""Tests for JiraClient (with mocked HTTP)."""

import os
from unittest.mock import MagicMock, patch

import pytest

from agentic_ci.jira.client import JiraClient, JiraError


@pytest.fixture()
def client():
    with patch("agentic_ci.jira.client.acli_mod.is_available", return_value=False):
        return JiraClient("https://test.atlassian.net", "user@test.com", "tok123")


class TestFromEnv:
    def test_missing_url_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="JIRA_URL"):
                JiraClient.from_env()

    def test_missing_creds_raises(self):
        with patch.dict(os.environ, {"JIRA_URL": "https://x.atlassian.net"}, clear=True):
            with pytest.raises(RuntimeError, match="JIRA_EMAIL"):
                JiraClient.from_env()

    def test_success(self):
        env = {
            "JIRA_URL": "https://x.atlassian.net",
            "JIRA_EMAIL": "a@b.com",
            "JIRA_API_TOKEN": "tok",
        }
        with patch.dict(os.environ, env, clear=True):
            c = JiraClient.from_env()
            assert c.url == "https://x.atlassian.net"
            assert c.auth == ("a@b.com", "tok")

    def test_url_param_overrides_env(self):
        env = {
            "JIRA_URL": "https://wrong.atlassian.net",
            "JIRA_EMAIL": "a@b.com",
            "JIRA_API_TOKEN": "tok",
        }
        with patch.dict(os.environ, env, clear=True):
            c = JiraClient.from_env(url="https://right.atlassian.net")
            assert c.url == "https://right.atlassian.net"


class TestGetIssue:
    @patch("agentic_ci.jira.client.requests")
    def test_get_issue_basic(self, mock_requests, client):
        issue_resp = MagicMock()
        issue_resp.status_code = 200
        issue_resp.json.return_value = {
            "key": "TEST-1",
            "fields": {
                "summary": "Fix bug",
                "description": "Some desc",
                "issuetype": {"name": "Bug"},
                "labels": ["autofix"],
                "status": {"name": "Open"},
                "reporter": {"displayName": "John", "emailAddress": "j@test.com"},
                "components": [{"name": "core"}],
                "project": {"key": "TEST"},
            },
        }

        comment_resp = MagicMock()
        comment_resp.status_code = 200
        comment_resp.json.return_value = {"comments": []}

        mock_requests.get.side_effect = [issue_resp, comment_resp]

        result = client.get_issue("TEST-1")
        assert result["key"] == "TEST-1"
        assert result["summary"] == "Fix bug"
        assert result["reporter_email"] == "j@test.com"
        assert result["labels"] == ["autofix"]


class TestSearch:
    @patch("agentic_ci.jira.client.requests")
    def test_search_basic(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "issues": [
                {
                    "key": "TEST-1",
                    "fields": {
                        "summary": "Bug 1",
                        "description": "desc",
                        "issuetype": {"name": "Bug"},
                        "labels": [],
                        "status": {"name": "Open"},
                        "comment": {"comments": []},
                    },
                }
            ],
            "isLast": True,
        }
        mock_requests.post.return_value = resp

        results = client.search("project = TEST")
        assert len(results) == 1
        assert results[0]["key"] == "TEST-1"


class TestEditLabels:
    @patch("agentic_ci.jira.client.requests")
    def test_add_labels(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 204
        mock_requests.put.return_value = resp

        client.edit_labels("TEST-1", add=["bug", "urgent"])
        mock_requests.put.assert_called_once()
        call_json = mock_requests.put.call_args.kwargs["json"]
        label_ops = call_json["update"]["labels"]
        assert {"add": "bug"} in label_ops
        assert {"add": "urgent"} in label_ops

    @patch("agentic_ci.jira.client.requests")
    def test_remove_labels(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 204
        mock_requests.put.return_value = resp

        client.edit_labels("TEST-1", remove=["stale"])
        call_json = mock_requests.put.call_args.kwargs["json"]
        label_ops = call_json["update"]["labels"]
        assert {"remove": "stale"} in label_ops

    @patch("agentic_ci.jira.client.requests")
    def test_noop_when_empty(self, mock_requests, client):
        client.edit_labels("TEST-1", add=None, remove=None)
        mock_requests.put.assert_not_called()
        mock_requests.post.assert_not_called()
        mock_requests.get.assert_not_called()


class TestAddComment:
    @patch("agentic_ci.jira.client.requests")
    def test_comment_success(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 201
        mock_requests.post.return_value = resp

        assert client.add_comment("TEST-1", "Fixed it") is True

    @patch("agentic_ci.jira.client.requests")
    def test_comment_with_visibility(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 201
        mock_requests.post.return_value = resp

        client.add_comment("TEST-1", "Internal", visibility_group="Red Hat Employee")
        call_json = mock_requests.post.call_args.kwargs["json"]
        assert call_json["visibility"]["value"] == "Red Hat Employee"

    @patch("agentic_ci.jira.client.requests")
    def test_comment_failure(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 403
        mock_requests.post.return_value = resp

        assert client.add_comment("TEST-1", "Nope") is False


class TestTransition:
    @patch("agentic_ci.jira.client.requests")
    def test_transition_success(self, mock_requests, client):
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {
            "transitions": [
                {"id": "31", "name": "In Progress", "to": {"name": "In Progress"}},
                {"id": "41", "name": "Done", "to": {"name": "Done"}},
            ]
        }

        post_resp = MagicMock()
        post_resp.status_code = 204

        mock_requests.get.return_value = get_resp
        mock_requests.post.return_value = post_resp

        client.transition("TEST-1", "Done")
        call_json = mock_requests.post.call_args.kwargs["json"]
        assert call_json["transition"]["id"] == "41"

    @patch("agentic_ci.jira.client.requests")
    def test_transition_not_found(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"transitions": []}
        mock_requests.get.return_value = resp

        with pytest.raises(JiraError, match="No transition"):
            client.transition("TEST-1", "Nonexistent")


class TestCreateIssue:
    @patch("agentic_ci.jira.client.requests")
    def test_create_basic(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"key": "TEST-42"}
        mock_requests.post.return_value = resp

        key = client.create_issue("TEST", "Bug", "Something broke")
        assert key == "TEST-42"
