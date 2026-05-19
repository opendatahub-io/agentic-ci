"""Tests for acli-backed JiraClient operations."""

from unittest.mock import MagicMock, patch

import pytest

from agentic_ci.jira.acli import AcliError
from agentic_ci.jira.client import JiraClient


@pytest.fixture()
def acli_client():
    """JiraClient with acli detected as available."""
    with patch("agentic_ci.jira.client.acli_mod.is_available", return_value=True):
        return JiraClient("https://test.atlassian.net", "user@test.com", "tok123")


@pytest.fixture()
def rest_client():
    """JiraClient with acli NOT available (pure REST)."""
    with patch("agentic_ci.jira.client.acli_mod.is_available", return_value=False):
        return JiraClient("https://test.atlassian.net", "user@test.com", "tok123")


class TestAcliDetection:
    def test_acli_available(self, acli_client):
        assert acli_client._acli_available is True

    def test_acli_not_available(self, rest_client):
        assert rest_client._acli_available is False


class TestTransitionAcli:
    @patch("agentic_ci.jira.client.acli_mod.run_acli")
    def test_uses_acli_when_available(self, mock_run, acli_client):
        acli_client.transition("TEST-1", "Done")
        mock_run.assert_called_once_with(
            "jira",
            "workitem",
            "transition",
            "--key",
            "TEST-1",
            "--status",
            "Done",
        )

    @patch("agentic_ci.jira.client.requests")
    @patch("agentic_ci.jira.client.acli_mod.run_acli", side_effect=AcliError("fail"))
    def test_falls_back_to_rest_on_acli_error(self, mock_acli, mock_requests, acli_client):
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {
            "transitions": [{"id": "41", "name": "Done", "to": {"name": "Done"}}]
        }
        post_resp = MagicMock()
        post_resp.status_code = 204
        mock_requests.get.return_value = get_resp
        mock_requests.post.return_value = post_resp

        acli_client.transition("TEST-1", "Done")
        mock_requests.post.assert_called_once()

    @patch("agentic_ci.jira.client.requests")
    def test_rest_only_client_skips_acli(self, mock_requests, rest_client):
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {
            "transitions": [{"id": "41", "name": "Done", "to": {"name": "Done"}}]
        }
        post_resp = MagicMock()
        post_resp.status_code = 204
        mock_requests.get.return_value = get_resp
        mock_requests.post.return_value = post_resp

        rest_client.transition("TEST-1", "Done")
        mock_requests.get.assert_called_once()


class TestAssignAcli:
    @patch("agentic_ci.jira.client.acli_mod.run_acli")
    def test_uses_acli(self, mock_run, acli_client):
        acli_client.assign("TEST-1", "user@test.com")
        mock_run.assert_called_once_with(
            "jira",
            "workitem",
            "assign",
            "--key",
            "TEST-1",
            "--assignee",
            "user@test.com",
        )


class TestCommentAcli:
    @patch("agentic_ci.jira.client.acli_mod.run_acli")
    def test_uses_acli_without_visibility(self, mock_run, acli_client):
        result = acli_client.add_comment("TEST-1", "Fixed it")
        assert result is True
        mock_run.assert_called_once_with(
            "jira",
            "workitem",
            "comment",
            "create",
            "--key",
            "TEST-1",
            "--body",
            "Fixed it",
        )

    @patch("agentic_ci.jira.client.requests")
    def test_uses_rest_with_visibility(self, mock_requests, acli_client):
        resp = MagicMock()
        resp.status_code = 201
        mock_requests.post.return_value = resp

        acli_client.add_comment("TEST-1", "Secret", visibility_group="Red Hat Employee")
        call_json = mock_requests.post.call_args.kwargs["json"]
        assert call_json["visibility"]["value"] == "Red Hat Employee"


class TestLinkAcli:
    @patch("agentic_ci.jira.client.acli_mod.run_acli")
    def test_uses_acli(self, mock_run, acli_client):
        acli_client.link_issues("TEST-1", "TEST-2", "Blocks")
        mock_run.assert_called_once_with(
            "jira",
            "workitem",
            "link",
            "create",
            "--out",
            "TEST-1",
            "--in",
            "TEST-2",
            "--type",
            "Blocks",
        )


class TestEditLabelsAcli:
    @patch("agentic_ci.jira.client.acli_mod.run_acli")
    def test_add_only_uses_acli(self, mock_run, acli_client):
        acli_client.edit_labels("TEST-1", add=["bug", "urgent"])
        mock_run.assert_called_once_with(
            "jira",
            "workitem",
            "edit",
            "--key",
            "TEST-1",
            "--labels",
            "bug,urgent",
        )

    @patch("agentic_ci.jira.client.requests")
    def test_remove_uses_rest(self, mock_requests, acli_client):
        resp = MagicMock()
        resp.status_code = 204
        mock_requests.put.return_value = resp

        acli_client.edit_labels("TEST-1", remove=["stale"])
        mock_requests.put.assert_called_once()

    @patch("agentic_ci.jira.client.requests")
    def test_mixed_add_remove_uses_rest(self, mock_requests, acli_client):
        resp = MagicMock()
        resp.status_code = 204
        mock_requests.put.return_value = resp

        acli_client.edit_labels("TEST-1", add=["new"], remove=["old"])
        mock_requests.put.assert_called_once()


class TestCreateIssueAcli:
    @patch("agentic_ci.jira.client.acli_mod.run_acli")
    def test_simple_create_uses_acli(self, mock_run, acli_client):
        mock_run.return_value = MagicMock(stdout='{"key": "TEST-42"}')
        key = acli_client.create_issue("TEST", "Bug", "Something broke")
        assert key == "TEST-42"

    @patch("agentic_ci.jira.client.requests")
    def test_create_with_epic_uses_rest(self, mock_requests, acli_client):
        field_resp = MagicMock()
        field_resp.status_code = 200
        field_resp.json.return_value = [{"name": "Epic Link", "id": "customfield_10014"}]

        create_resp = MagicMock()
        create_resp.status_code = 201
        create_resp.json.return_value = {"key": "TEST-43"}

        mock_requests.get.return_value = field_resp
        mock_requests.post.return_value = create_resp

        key = acli_client.create_issue("TEST", "Bug", "Bug", parent_epic="EPIC-1")
        assert key == "TEST-43"
