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
                "created": "2026-08-01T12:34:56.000+0000",
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
        assert result["created"] == "2026-08-01T12:34:56.000+0000"
        assert result["reporter_email"] == "j@test.com"
        assert result["labels"] == ["autofix"]
        assert "created" in mock_requests.get.call_args_list[0].args[0]

    @patch("agentic_ci.jira.client.requests")
    def test_get_issue_missing_created_defaults_to_empty_string(self, mock_requests, client):
        issue_resp = MagicMock()
        issue_resp.status_code = 200
        issue_resp.json.return_value = {"key": "TEST-1", "fields": {}}

        comment_resp = MagicMock()
        comment_resp.status_code = 200
        comment_resp.json.return_value = {"comments": []}

        mock_requests.get.side_effect = [issue_resp, comment_resp]

        result = client.get_issue("TEST-1")

        assert result["created"] == ""

    @patch("agentic_ci.jira.client.requests")
    def test_fetch_comments_includes_updated(self, mock_requests, client):
        issue_resp = MagicMock()
        issue_resp.status_code = 200
        issue_resp.json.return_value = {
            "key": "TEST-1",
            "fields": {
                "summary": "s",
                "description": "",
                "issuetype": {"name": "Bug"},
                "labels": [],
                "status": {"name": "Open"},
                "reporter": {},
                "components": [],
                "project": {"key": "TEST"},
            },
        }

        comment_resp = MagicMock()
        comment_resp.status_code = 200
        comment_resp.json.return_value = {
            "comments": [
                {
                    "id": "100",
                    "author": {"displayName": "Bot", "emailAddress": "bot@test.com"},
                    "body": "initial comment",
                    "created": "2026-07-06T18:00:00.000+0000",
                    "updated": "2026-07-06T22:43:38.000+0000",
                }
            ]
        }

        mock_requests.get.side_effect = [issue_resp, comment_resp]

        result = client.get_issue("TEST-1")
        assert len(result["comments"]) == 1
        assert result["comments"][0]["created"] == "2026-07-06T18:00:00.000+0000"
        assert result["comments"][0]["updated"] == "2026-07-06T22:43:38.000+0000"


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
                        "created": "2026-08-02T12:34:56.000+0000",
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
        assert results[0]["created"] == "2026-08-02T12:34:56.000+0000"
        assert "created" in mock_requests.post.call_args.kwargs["json"]["fields"]

    @patch("agentic_ci.jira.client.requests")
    def test_search_missing_created_defaults_to_empty_string(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "issues": [{"key": "TEST-1", "fields": {}}],
            "isLast": True,
        }
        mock_requests.post.return_value = resp

        results = client.search("project = TEST")

        assert results[0]["created"] == ""

    @staticmethod
    def _page(issues, *, next_token=None):
        resp = MagicMock()
        resp.status_code = 200
        payload: dict = {"issues": issues, "isLast": next_token is None}
        if next_token is not None:
            payload["nextPageToken"] = next_token
        resp.json.return_value = payload
        return resp

    @patch("agentic_ci.jira.client.requests")
    def test_default_call_request_and_output_unchanged(self, mock_requests, client):
        mock_requests.post.return_value = self._page(
            [
                {
                    "key": "TEST-1",
                    "fields": {
                        "summary": "Bug 1",
                        "description": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "desc"}],
                                }
                            ],
                        },
                        "created": "2026-08-02T12:34:56.000+0000",
                        "issuetype": {"name": "Bug"},
                        "labels": ["a"],
                        "status": {"name": "Open"},
                        "comment": {
                            "comments": [
                                {
                                    "id": "10",
                                    "author": {
                                        "displayName": "Ann",
                                        "emailAddress": "ann@test.com",
                                    },
                                    "body": "hi",
                                    "created": "c",
                                    "updated": "u",
                                }
                            ]
                        },
                    },
                }
            ]
        )

        results = client.search("project = TEST")

        assert mock_requests.post.call_count == 1
        assert mock_requests.post.call_args.args[0] == (
            "https://test.atlassian.net/rest/api/3/search/jql"
        )
        assert mock_requests.post.call_args.kwargs["json"] == {
            "jql": "project = TEST",
            "fields": [
                "summary",
                "description",
                "created",
                "issuetype",
                "labels",
                "comment",
                "status",
            ],
            "maxResults": 50,
        }
        assert results == [
            {
                "key": "TEST-1",
                "summary": "Bug 1",
                "description": "desc",
                "created": "2026-08-02T12:34:56.000+0000",
                "issue_type": "Bug",
                "labels": ["a"],
                "status": "Open",
                "comments": [
                    {
                        "id": "10",
                        "author": "Ann",
                        "author_email": "ann@test.com",
                        "body": "hi",
                        "created": "c",
                        "updated": "u",
                        "visibility": None,
                    }
                ],
            }
        ]

    @patch("agentic_ci.jira.client.requests")
    def test_default_call_pages_at_50(self, mock_requests, client):
        mock_requests.post.side_effect = [
            self._page([{"key": f"TEST-{i}"} for i in range(50)], next_token="t1"),
            self._page([{"key": "TEST-50"}]),
        ]

        results = client.search("project = TEST", max_results=60)

        assert len(results) == 51
        payloads = [c.kwargs["json"] for c in mock_requests.post.call_args_list]
        assert [p["maxResults"] for p in payloads] == [50, 10]
        assert "nextPageToken" not in payloads[0]
        assert payloads[1]["nextPageToken"] == "t1"

    @patch("agentic_ci.jira.client.requests")
    def test_custom_fields_requested_and_missing_fields_tolerated(self, mock_requests, client):
        mock_requests.post.return_value = self._page(
            [{"id": "1", "key": "TEST-1", "fields": {"summary": "Bug 1"}}]
        )

        results = client.search("project = TEST", fields=["summary"])

        payload = mock_requests.post.call_args.kwargs["json"]
        assert payload["fields"] == ["summary"]
        assert payload["maxResults"] == 50
        assert results == [
            {
                "key": "TEST-1",
                "summary": "Bug 1",
                "description": "",
                "created": "",
                "issue_type": "",
                "labels": [],
                "status": "",
                "comments": [],
            }
        ]

    @patch("agentic_ci.jira.client.requests")
    def test_key_only_fields_use_largest_page(self, mock_requests, client):
        mock_requests.post.return_value = self._page([{"id": "1", "key": "TEST-1"}])

        results = client.search("project = TEST", max_results=10000, fields=["key"])

        payload = mock_requests.post.call_args.kwargs["json"]
        assert payload["fields"] == ["key"]
        assert payload["maxResults"] == 5000
        assert results[0]["key"] == "TEST-1"
        assert results[0]["comments"] == []

    @patch("agentic_ci.jira.client.requests")
    def test_single_field_string_not_split_into_characters(self, mock_requests, client):
        mock_requests.post.return_value = self._page([{"id": "1", "key": "TEST-1"}])

        client.search("project = TEST", fields="key")

        payload = mock_requests.post.call_args.kwargs["json"]
        assert payload["fields"] == ["key"]
        assert payload["maxResults"] == 500

    @patch("agentic_ci.jira.client.requests")
    def test_null_fields_tolerated(self, mock_requests, client):
        mock_requests.post.return_value = self._page(
            [
                {"key": "TEST-1", "fields": None},
                {
                    "key": "TEST-2",
                    "fields": {"issuetype": None, "status": None, "comment": None},
                },
            ]
        )

        results = client.search("project = TEST")

        assert [r["key"] for r in results] == ["TEST-1", "TEST-2"]
        assert all(r["issue_type"] == "" and r["status"] == "" for r in results)
        assert all(r["comments"] == [] for r in results)


class TestSearchKeys:
    @staticmethod
    def _page(keys, *, next_token=None):
        resp = MagicMock()
        resp.status_code = 200
        payload: dict = {
            "issues": [{"id": str(i), "key": k} for i, k in enumerate(keys)],
            "isLast": next_token is None,
        }
        if next_token is not None:
            payload["nextPageToken"] = next_token
        resp.json.return_value = payload
        return resp

    @patch("agentic_ci.jira.client.requests")
    def test_pages_with_next_page_token(self, mock_requests, client):
        mock_requests.post.side_effect = [
            self._page(["TEST-1", "TEST-2"], next_token="t1"),
            self._page(["TEST-3"], next_token="t2"),
            self._page(["TEST-4"]),
        ]

        keys = client.search_keys("project = TEST", max_results=20000)

        assert keys == ["TEST-1", "TEST-2", "TEST-3", "TEST-4"]
        payloads = [c.kwargs["json"] for c in mock_requests.post.call_args_list]
        assert len(payloads) == 3
        for p in payloads:
            assert p["jql"] == "project = TEST"
            assert p["fields"] == ["key"]
        assert [p["maxResults"] for p in payloads] == [5000, 5000, 5000]
        assert "nextPageToken" not in payloads[0]
        assert [p["nextPageToken"] for p in payloads[1:]] == ["t1", "t2"]

    @patch("agentic_ci.jira.client.requests")
    def test_default_max_results_is_one_full_page(self, mock_requests, client):
        mock_requests.post.return_value = self._page(["TEST-1"])

        client.search_keys("project = TEST")

        assert mock_requests.post.call_args.kwargs["json"]["maxResults"] == 5000

    @patch("agentic_ci.jira.client.requests")
    def test_max_results_truncates_and_stops_paging(self, mock_requests, client):
        mock_requests.post.side_effect = [
            self._page(["TEST-1", "TEST-2"], next_token="t1"),
            self._page(["TEST-3", "TEST-4", "TEST-5"], next_token="t2"),
            self._page(["TEST-6"]),
        ]

        keys = client.search_keys("project = TEST", max_results=3)

        assert keys == ["TEST-1", "TEST-2", "TEST-3"]
        assert mock_requests.post.call_count == 2
        payloads = [c.kwargs["json"] for c in mock_requests.post.call_args_list]
        assert [p["maxResults"] for p in payloads] == [3, 1]

    @patch("agentic_ci.jira.client.requests")
    def test_stops_when_token_missing(self, mock_requests, client):
        resp = self._page(["TEST-1"])
        resp.json.return_value["isLast"] = False
        mock_requests.post.return_value = resp

        assert client.search_keys("project = TEST") == ["TEST-1"]
        assert mock_requests.post.call_count == 1

    @patch("agentic_ci.jira.client.requests")
    def test_issues_without_key_skipped(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "issues": [{"id": "1"}, {"id": "2", "key": "TEST-2", "fields": {}}, {"key": None}],
            "isLast": True,
        }
        mock_requests.post.return_value = resp

        assert client.search_keys("project = TEST") == ["TEST-2"]


class TestGetIssueLinks:
    @staticmethod
    def _resp(payload):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload
        return resp

    @patch("agentic_ci.jira.client.requests")
    def test_normalises_inward_and_outward(self, mock_requests, client):
        mock_requests.get.return_value = self._resp(
            {
                "fields": {
                    "issuelinks": [
                        {
                            "type": {"name": "Blocks", "inward": "is blocked by"},
                            "inwardIssue": {
                                "key": "TEST-2",
                                "fields": {"status": {"name": "Open"}},
                            },
                        },
                        {
                            "type": {"name": "Blocks", "outward": "blocks"},
                            "outwardIssue": {
                                "key": "TEST-3",
                                "fields": {"status": {"name": "Done"}},
                            },
                        },
                    ]
                }
            }
        )

        links = client.get_issue_links("TEST-1")
        assert links == [
            {"type": "Blocks", "direction": "inward", "key": "TEST-2", "status": "Open"},
            {"type": "Blocks", "direction": "outward", "key": "TEST-3", "status": "Done"},
        ]
        assert "fields=issuelinks" in mock_requests.get.call_args.args[0]

    @patch("agentic_ci.jira.client.requests")
    def test_missing_status_defaults_to_empty(self, mock_requests, client):
        mock_requests.get.return_value = self._resp(
            {
                "fields": {
                    "issuelinks": [{"type": {"name": "Relates"}, "outwardIssue": {"key": "TEST-9"}}]
                }
            }
        )

        links = client.get_issue_links("TEST-1")
        assert links == [{"type": "Relates", "direction": "outward", "key": "TEST-9", "status": ""}]

    @patch("agentic_ci.jira.client.requests")
    def test_no_links_returns_empty(self, mock_requests, client):
        mock_requests.get.return_value = self._resp({"fields": {}})
        assert client.get_issue_links("TEST-1") == []

    @patch("agentic_ci.jira.client.requests")
    def test_null_issuelinks_returns_empty(self, mock_requests, client):
        mock_requests.get.return_value = self._resp({"fields": {"issuelinks": None}})
        assert client.get_issue_links("TEST-1") == []

    @patch("agentic_ci.jira.client.requests")
    def test_api_error_raises(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "not found"
        mock_requests.get.return_value = resp

        with pytest.raises(JiraError):
            client.get_issue_links("TEST-1")


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


class TestUpdateComment:
    @patch("agentic_ci.jira.client.requests")
    def test_update_success(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        mock_requests.put.return_value = resp

        assert client.update_comment("TEST-1", "10001", "Updated text") is True

    @patch("agentic_ci.jira.client.requests")
    def test_update_with_visibility(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 200
        mock_requests.put.return_value = resp

        client.update_comment("TEST-1", "10001", "Internal", visibility_group="Red Hat Employee")
        call_json = mock_requests.put.call_args.kwargs["json"]
        assert call_json["visibility"]["value"] == "Red Hat Employee"

    @patch("agentic_ci.jira.client.requests")
    def test_update_failure(self, mock_requests, client):
        resp = MagicMock()
        resp.status_code = 403
        mock_requests.put.return_value = resp

        assert client.update_comment("TEST-1", "10001", "Nope") is False


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


class TestGetLabelAuthor:
    @staticmethod
    def _changelog_resp(values, total=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "values": values,
            "total": total if total is not None else len(values),
        }
        return resp

    @staticmethod
    def _issue_resp(labels, reporter=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "fields": {
                "labels": labels,
                "reporter": reporter or {},
            }
        }
        return resp

    @patch("agentic_ci.jira.client.requests")
    def test_latest_add_timestamp_across_pages(self, mock_requests, client):
        page1 = self._changelog_resp(
            [
                {
                    "author": {"emailAddress": "a@test.com", "displayName": "Alice"},
                    "created": "2026-06-01T10:00:00.000+0000",
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
            ],
            total=2,
        )
        page2 = self._changelog_resp(
            [
                {
                    "author": {"emailAddress": "b@test.com", "displayName": "Bob"},
                    "created": "2026-07-15T14:30:00.000+0000",
                    "items": [
                        {
                            "field": "labels",
                            "fromString": "other",
                            "toString": "other autofix",
                        },
                    ],
                },
            ],
            total=2,
        )
        mock_requests.get.side_effect = [page1, page2]

        result = client.get_label_author("TEST-1", "autofix")
        assert result["found"] is True
        assert result["email"] == "b@test.com"
        assert result["displayName"] == "Bob"
        assert result["added_at"] == "2026-07-15T14:30:00+00:00"

    @patch("agentic_ci.jira.client.requests")
    def test_remove_and_readd_resets_timestamp(self, mock_requests, client):
        changelog = self._changelog_resp(
            [
                {
                    "author": {"emailAddress": "a@test.com", "displayName": "Alice"},
                    "created": "2026-06-01T10:00:00.000+0000",
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
                {
                    "author": {"emailAddress": "a@test.com", "displayName": "Alice"},
                    "created": "2026-06-05T12:00:00.000+0000",
                    "items": [
                        {"field": "labels", "fromString": "autofix", "toString": ""},
                    ],
                },
                {
                    "author": {"emailAddress": "c@test.com", "displayName": "Carol"},
                    "created": "2026-08-20T09:15:00.000+0000",
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
            ],
        )
        mock_requests.get.side_effect = [changelog]

        result = client.get_label_author("TEST-1", "autofix")
        assert result["found"] is True
        assert result["email"] == "c@test.com"
        assert result["displayName"] == "Carol"
        assert result["added_at"] == "2026-08-20T09:15:00+00:00"

    @patch("agentic_ci.jira.client.requests")
    def test_unrelated_changes_do_not_affect_timestamp(self, mock_requests, client):
        changelog = self._changelog_resp(
            [
                {
                    "author": {"emailAddress": "a@test.com", "displayName": "Alice"},
                    "created": "2026-06-01T10:00:00.000+0000",
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
                {
                    "author": {"emailAddress": "x@test.com", "displayName": "Xavier"},
                    "created": "2026-09-01T08:00:00.000+0000",
                    "items": [
                        {"field": "summary", "fromString": "old", "toString": "new"},
                    ],
                },
                {
                    "author": {"emailAddress": "y@test.com", "displayName": "Yara"},
                    "created": "2026-09-02T08:00:00.000+0000",
                    "items": [
                        {
                            "field": "labels",
                            "fromString": "autofix",
                            "toString": "autofix other",
                        },
                    ],
                },
            ],
        )
        mock_requests.get.side_effect = [changelog]

        result = client.get_label_author("TEST-1", "autofix")
        assert result["found"] is True
        assert result["email"] == "a@test.com"
        assert result["added_at"] == "2026-06-01T10:00:00+00:00"

    @patch("agentic_ci.jira.client.requests")
    def test_reporter_fallback_returns_null_added_at(self, mock_requests, client):
        changelog = self._changelog_resp([], total=0)
        issue = self._issue_resp(
            ["autofix"],
            reporter={"emailAddress": "r@test.com", "displayName": "Reporter"},
        )
        mock_requests.get.side_effect = [changelog, issue]

        result = client.get_label_author("TEST-1", "autofix")
        assert result["found"] is True
        assert result["email"] == "r@test.com"
        assert result["displayName"] == "Reporter"
        assert result["added_at"] is None

    @patch("agentic_ci.jira.client.requests")
    def test_missing_timestamp_returns_null_added_at(self, mock_requests, client):
        changelog = self._changelog_resp(
            [
                {
                    "author": {"emailAddress": "a@test.com", "displayName": "Alice"},
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
            ],
        )
        mock_requests.get.side_effect = [changelog]

        result = client.get_label_author("TEST-1", "autofix")
        assert result["found"] is True
        assert result["email"] == "a@test.com"
        assert result["added_at"] is None

    @patch("agentic_ci.jira.client.requests")
    def test_invalid_timestamp_returns_null_added_at(self, mock_requests, client):
        changelog = self._changelog_resp(
            [
                {
                    "author": {"emailAddress": "a@test.com", "displayName": "Alice"},
                    "created": "not-a-date",
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
            ],
        )
        mock_requests.get.side_effect = [changelog]

        result = client.get_label_author("TEST-1", "autofix")
        assert result["found"] is True
        assert result["email"] == "a@test.com"
        assert result["added_at"] is None

    @patch("agentic_ci.jira.client.requests")
    def test_not_found_unchanged(self, mock_requests, client):
        changelog = self._changelog_resp([], total=0)
        issue = self._issue_resp([])
        mock_requests.get.side_effect = [changelog, issue]

        result = client.get_label_author("TEST-1", "autofix")
        assert result == {"found": False}

    @patch("agentic_ci.jira.client.requests")
    def test_author_aligns_with_selected_event(self, mock_requests, client):
        changelog = self._changelog_resp(
            [
                {
                    "author": {"emailAddress": "first@test.com", "displayName": "First"},
                    "created": "2026-06-01T10:00:00.000+0000",
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
                {
                    "author": {"emailAddress": "first@test.com", "displayName": "First"},
                    "created": "2026-06-05T12:00:00.000+0000",
                    "items": [
                        {"field": "labels", "fromString": "autofix", "toString": ""},
                    ],
                },
                {
                    "author": {"emailAddress": "second@test.com", "displayName": "Second"},
                    "created": "2026-07-01T08:00:00.000+0000",
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
            ],
        )
        mock_requests.get.side_effect = [changelog]

        result = client.get_label_author("TEST-1", "autofix")
        assert result["found"] is True
        assert result["email"] == "second@test.com"
        assert result["displayName"] == "Second"
        assert result["added_at"] == "2026-07-01T08:00:00+00:00"

    @patch("agentic_ci.jira.client.requests")
    def test_naive_timestamp_returns_null(self, mock_requests, client):
        changelog = self._changelog_resp(
            [
                {
                    "author": {"emailAddress": "a@test.com", "displayName": "Alice"},
                    "created": "2026-06-01T10:00:00",
                    "items": [
                        {"field": "labels", "fromString": "", "toString": "autofix"},
                    ],
                },
            ],
        )
        mock_requests.get.side_effect = [changelog]

        result = client.get_label_author("TEST-1", "autofix")
        assert result["found"] is True
        assert result["added_at"] is None
