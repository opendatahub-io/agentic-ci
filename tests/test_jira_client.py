"""Tests for JiraClient (with mocked HTTP)."""

import os
from unittest.mock import MagicMock, call, patch

import pytest

from agentic_ci.jira.client import MAX_RETRY_AFTER, JiraClient, JiraError


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


class TestRetryOn429:
    @patch("time.sleep")
    @patch("agentic_ci.jira.client.random.uniform", return_value=0.0)
    @patch("agentic_ci.jira.client.requests")
    def test_retries_on_429_then_succeeds(self, mock_requests, _mock_rand, mock_sleep, client):
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"key": "TEST-1", "fields": {}}

        comment_resp = MagicMock()
        comment_resp.status_code = 200
        comment_resp.json.return_value = {"comments": []}

        mock_requests.get.side_effect = [rate_resp, ok_resp, comment_resp]

        result = client.get_issue("TEST-1")
        assert result["key"] == "TEST-1"
        assert mock_requests.get.call_count == 3
        mock_sleep.assert_called_once_with(1.0)

    @patch("time.sleep")
    @patch("agentic_ci.jira.client.random.uniform", return_value=0.0)
    @patch("agentic_ci.jira.client.requests")
    def test_respects_retry_after_header(self, mock_requests, _mock_rand, mock_sleep, client):
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "5"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"comments": []}

        mock_requests.get.side_effect = [rate_resp, ok_resp]

        client._request("get", "https://test.atlassian.net/rest/api/3/test")
        mock_sleep.assert_called_once_with(5.0)

    @patch("time.sleep")
    @patch("agentic_ci.jira.client.random.uniform", return_value=0.0)
    @patch("agentic_ci.jira.client.requests")
    def test_gives_up_after_max_retries(self, mock_requests, _mock_rand, mock_sleep, client):
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {}
        rate_resp.text = "Rate limited"

        mock_requests.get.return_value = rate_resp

        with pytest.raises(JiraError, match="429"):
            client.get_issue("TEST-1")

        assert mock_requests.get.call_count == 5
        assert mock_sleep.call_count == 4

    @patch("time.sleep")
    @patch("agentic_ci.jira.client.random.uniform", return_value=0.0)
    @patch("agentic_ci.jira.client.requests")
    def test_exponential_backoff_delays(self, mock_requests, _mock_rand, mock_sleep, client):
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"comments": []}

        mock_requests.get.side_effect = [rate_resp, rate_resp, rate_resp, ok_resp]

        resp = client._request("get", "https://test.atlassian.net/rest/api/3/test")
        assert resp.status_code == 200
        assert mock_sleep.call_args_list == [call(1.0), call(2.0), call(4.0)]

    @patch("time.sleep")
    @patch("agentic_ci.jira.client.random.uniform", return_value=0.0)
    @patch("agentic_ci.jira.client.requests")
    def test_no_retry_on_non_429_errors(self, mock_requests, _mock_rand, mock_sleep, client):
        err_resp = MagicMock()
        err_resp.status_code = 500
        err_resp.text = "Server error"
        mock_requests.get.return_value = err_resp

        with pytest.raises(JiraError, match="500"):
            client.get_issue("TEST-1")

        assert mock_requests.get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("agentic_ci.jira.client.random.uniform", return_value=0.0)
    @patch("agentic_ci.jira.client.requests")
    def test_retry_after_capped_at_maximum(self, mock_requests, _mock_rand, mock_sleep, client):
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "99999"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"comments": []}

        mock_requests.get.side_effect = [rate_resp, ok_resp]

        resp = client._request("get", "https://test.atlassian.net/rest/api/3/test")
        assert resp.status_code == 200
        mock_sleep.assert_called_once_with(float(MAX_RETRY_AFTER))

    @patch("time.sleep")
    @patch("agentic_ci.jira.client.random.uniform", return_value=0.0)
    @patch("agentic_ci.jira.client.requests")
    def test_retry_after_zero_uses_backoff_floor(
        self, mock_requests, _mock_rand, mock_sleep, client
    ):
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "0"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"comments": []}

        mock_requests.get.side_effect = [rate_resp, ok_resp]

        resp = client._request("get", "https://test.atlassian.net/rest/api/3/test")
        assert resp.status_code == 200
        mock_sleep.assert_called_once_with(1.0)


class TestGetDescriptionEditors:
    @patch("agentic_ci.jira.client.requests")
    def test_no_edits_returns_empty(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"values": [], "total": 0}
        mock_requests.get.return_value = resp

        result = client.get_description_editors("TEST-1")
        assert result == []

    @patch("agentic_ci.jira.client.requests")
    def test_redhat_editor_returned(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "values": [
                {
                    "author": {
                        "emailAddress": "dev@redhat.com",
                        "accountId": "abc123",
                    },
                    "items": [{"field": "description", "fromString": "old", "toString": "new"}],
                }
            ],
            "total": 1,
        }
        mock_requests.get.return_value = resp

        result = client.get_description_editors("TEST-1")
        assert result == ["dev@redhat.com"]

    @patch("agentic_ci.jira.client.requests")
    def test_external_editor_returned(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "values": [
                {
                    "author": {
                        "emailAddress": "attacker@evil.com",
                        "accountId": "xyz789",
                    },
                    "items": [{"field": "description", "fromString": "old", "toString": "new"}],
                }
            ],
            "total": 1,
        }
        mock_requests.get.return_value = resp

        result = client.get_description_editors("TEST-1")
        assert result == ["attacker@evil.com"]

    @patch("agentic_ci.jira.client.requests")
    def test_missing_email_produces_sentinel(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "values": [
                {
                    "author": {"accountId": "hidden-user-42"},
                    "items": [{"field": "description", "fromString": "old", "toString": "new"}],
                }
            ],
            "total": 1,
        }
        mock_requests.get.return_value = resp

        result = client.get_description_editors("TEST-1")
        assert result == ["missing-email:hidden-user-42"]

    @pytest.mark.parametrize("email_value", [None, ""])
    @patch("agentic_ci.jira.client.requests")
    def test_null_or_empty_email_produces_sentinel(self, mock_requests, client, email_value):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "values": [
                {
                    "author": {
                        "emailAddress": email_value,
                        "accountId": "hidden-user-42",
                    },
                    "items": [{"field": "description"}],
                }
            ],
            "total": 1,
        }
        mock_requests.get.return_value = resp

        result = client.get_description_editors("TEST-1")
        assert result == ["missing-email:hidden-user-42"]

    @patch("agentic_ci.jira.client.requests")
    def test_non_description_changes_ignored(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "values": [
                {
                    "author": {"emailAddress": "dev@redhat.com"},
                    "items": [{"field": "summary", "fromString": "old", "toString": "new"}],
                },
                {
                    "author": {"emailAddress": "dev@redhat.com"},
                    "items": [{"field": "labels", "fromString": "", "toString": "bug"}],
                },
            ],
            "total": 2,
        }
        mock_requests.get.return_value = resp

        result = client.get_description_editors("TEST-1")
        assert result == []

    @patch("agentic_ci.jira.client.requests")
    def test_deduplicates_editors(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "values": [
                {
                    "author": {"emailAddress": "dev@redhat.com"},
                    "items": [{"field": "description"}],
                },
                {
                    "author": {"emailAddress": "dev@redhat.com"},
                    "items": [{"field": "description"}],
                },
                {
                    "author": {"emailAddress": "other@redhat.com"},
                    "items": [{"field": "description"}],
                },
            ],
            "total": 3,
        }
        mock_requests.get.return_value = resp

        result = client.get_description_editors("TEST-1")
        assert result == ["dev@redhat.com", "other@redhat.com"]

    @patch("agentic_ci.jira.client.requests")
    def test_paginates_changelog(self, mock_requests, client):
        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "values": [
                {
                    "author": {"emailAddress": "dev@redhat.com"},
                    "items": [{"field": "description"}],
                }
            ],
            "total": 2,
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "values": [
                {
                    "author": {"emailAddress": "attacker@evil.com"},
                    "items": [{"field": "description"}],
                }
            ],
            "total": 2,
        }
        mock_requests.get.side_effect = [page1, page2]

        result = client.get_description_editors("TEST-1")
        assert result == ["dev@redhat.com", "attacker@evil.com"]
        assert mock_requests.get.call_count == 2

    @pytest.mark.parametrize("bad_value", ["nan", "NaN", "inf", "-inf", "Infinity"])
    @patch("time.sleep")
    @patch("agentic_ci.jira.client.random.uniform", return_value=0.0)
    @patch("agentic_ci.jira.client.requests")
    def test_retry_after_non_finite_uses_backoff(
        self, mock_requests, _mock_rand, mock_sleep, client, bad_value
    ):
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": bad_value}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"comments": []}

        mock_requests.get.side_effect = [rate_resp, ok_resp]

        resp = client._request("get", "https://test.atlassian.net/rest/api/3/test")
        assert resp.status_code == 200
        mock_sleep.assert_called_once_with(1.0)


class TestSetSecurityLevel:
    @patch("agentic_ci.jira.client.requests")
    def test_set_security_level_success(self, mock_requests, client):
        issue_resp = MagicMock()
        issue_resp.status_code = 200
        issue_resp.json.return_value = {"fields": {"project": {"id": "10001"}}}

        levels_resp = MagicMock()
        levels_resp.status_code = 200
        levels_resp.json.return_value = {
            "levels": [
                {"id": "100", "name": "Internal"},
                {"id": "200", "name": "Confidential"},
            ]
        }

        put_resp = MagicMock()
        put_resp.status_code = 204

        mock_requests.get.side_effect = [issue_resp, levels_resp]
        mock_requests.put.return_value = put_resp

        client.set_security_level("TEST-1", "Confidential")

        mock_requests.put.assert_called_once()
        call_json = mock_requests.put.call_args.kwargs["json"]
        assert call_json == {"fields": {"security": {"id": "200"}}}

    @patch("agentic_ci.jira.client.requests")
    def test_set_security_level_case_insensitive(self, mock_requests, client):
        issue_resp = MagicMock()
        issue_resp.status_code = 200
        issue_resp.json.return_value = {"fields": {"project": {"id": "10001"}}}

        levels_resp = MagicMock()
        levels_resp.status_code = 200
        levels_resp.json.return_value = {"levels": [{"id": "100", "name": "Internal"}]}

        put_resp = MagicMock()
        put_resp.status_code = 204

        mock_requests.get.side_effect = [issue_resp, levels_resp]
        mock_requests.put.return_value = put_resp

        client.set_security_level("TEST-1", "internal")

        mock_requests.put.assert_called_once()
        call_json = mock_requests.put.call_args.kwargs["json"]
        assert call_json == {"fields": {"security": {"id": "100"}}}

    @patch("agentic_ci.jira.client.requests")
    def test_set_security_level_not_found(self, mock_requests, client):
        issue_resp = MagicMock()
        issue_resp.status_code = 200
        issue_resp.json.return_value = {"fields": {"project": {"id": "10001"}}}

        levels_resp = MagicMock()
        levels_resp.status_code = 200
        levels_resp.json.return_value = {"levels": [{"id": "100", "name": "Internal"}]}

        mock_requests.get.side_effect = [issue_resp, levels_resp]

        with pytest.raises(JiraError, match="Security level 'TopSecret' not found"):
            client.set_security_level("TEST-1", "TopSecret")


class TestResolveAccountId:
    @patch("agentic_ci.jira.client.requests")
    def test_resolve_account_id_email(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"accountId": "abc123", "displayName": "Test User"}]
        mock_requests.get.return_value = resp

        result = client._resolve_account_id("user@example.com")
        assert result == "abc123"
        mock_requests.get.assert_called_once()
        assert mock_requests.get.call_args.kwargs["params"] == {"query": "user@example.com"}

    @patch("agentic_ci.jira.client.requests")
    def test_resolve_account_id_passthrough(self, mock_requests, client):
        result = client._resolve_account_id("abc123")
        assert result == "abc123"
        mock_requests.get.assert_not_called()

    @patch("agentic_ci.jira.client.requests")
    def test_resolve_account_id_not_found(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = []
        mock_requests.get.return_value = resp

        with pytest.raises(JiraError, match="No Jira user found"):
            client._resolve_account_id("nobody@example.com")

    def test_resolve_account_id_empty_raises(self, client):
        with pytest.raises(JiraError, match="cannot be empty"):
            client._resolve_account_id("")

    def test_resolve_account_id_whitespace_raises(self, client):
        with pytest.raises(JiraError, match="cannot be empty"):
            client._resolve_account_id("   ")

    @patch("agentic_ci.jira.client.requests")
    def test_resolve_account_id_missing_key_raises(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"displayName": "Test User"}]
        mock_requests.get.return_value = resp

        with pytest.raises(JiraError, match="invalid response"):
            client._resolve_account_id("user@example.com")


class TestAssignRest:
    @patch("agentic_ci.jira.client.requests")
    def test_assign_rest_resolves_email(self, mock_requests, client):
        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = [{"accountId": "acct-456"}]

        assign_resp = MagicMock()
        assign_resp.status_code = 204

        mock_requests.get.return_value = search_resp
        mock_requests.put.return_value = assign_resp

        client.assign("TEST-1", "dev@example.com")

        mock_requests.put.assert_called_once()
        assert mock_requests.put.call_args.kwargs["json"] == {"accountId": "acct-456"}

    @patch("agentic_ci.jira.client.requests")
    def test_assign_rest_account_id_passthrough(self, mock_requests, client):
        assign_resp = MagicMock()
        assign_resp.status_code = 204
        mock_requests.put.return_value = assign_resp

        client.assign("TEST-1", "acct-456")

        mock_requests.get.assert_not_called()
        mock_requests.put.assert_called_once()
        assert mock_requests.put.call_args.kwargs["json"] == {"accountId": "acct-456"}
