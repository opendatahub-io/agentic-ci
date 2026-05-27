"""Tests for forge HTTP session and adapter configuration."""

from unittest.mock import MagicMock, patch

import pytest
from requests.adapters import HTTPAdapter

from agentic_ci.forge.session import (
    ForgeAuthError,
    GitHubHTTPAdapter,
    GitLabHTTPAdapter,
    build_session,
    extract_api_error,
)


class TestGitLabHTTPAdapter:
    def test_add_headers_injects_token(self, monkeypatch):
        monkeypatch.setenv("BOT_PAT", "glpat-test-token")
        adapter = GitLabHTTPAdapter()
        request = MagicMock()
        request.headers = {}
        adapter.add_headers(request)
        assert request.headers["PRIVATE-TOKEN"] == "glpat-test-token"

    def test_add_headers_exits_without_token(self, monkeypatch):
        monkeypatch.delenv("BOT_PAT", raising=False)
        adapter = GitLabHTTPAdapter()
        request = MagicMock()
        request.headers = {}
        with pytest.raises(ForgeAuthError):
            adapter.add_headers(request)

    def test_send_applies_default_timeout(self, monkeypatch):
        monkeypatch.setenv("BOT_PAT", "tok")
        adapter = GitLabHTTPAdapter()
        request = MagicMock()
        with patch.object(
            HTTPAdapter,
            "send",
            return_value=MagicMock(),
        ) as mock_send:
            adapter.send(request, timeout=None)
            call_args = mock_send.call_args
            timeout_val = call_args.kwargs.get(
                "timeout", call_args.args[1] if len(call_args.args) > 1 else None
            )
            assert timeout_val is not None


class TestGitHubHTTPAdapter:
    def test_add_headers_injects_bearer(self):
        adapter = GitHubHTTPAdapter(token="ghp-test-token")
        request = MagicMock()
        request.headers = {}
        adapter.add_headers(request)
        assert request.headers["Authorization"] == "Bearer ghp-test-token"
        assert request.headers["Accept"] == "application/vnd.github+json"

    def test_add_headers_exits_without_token(self):
        adapter = GitHubHTTPAdapter(token=None)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(ForgeAuthError):
            adapter.add_headers(request)

    def test_accept_header_not_overridden(self):
        adapter = GitHubHTTPAdapter(token="tok")
        request = MagicMock()
        request.headers = {"Accept": "application/json"}
        adapter.add_headers(request)
        assert request.headers["Accept"] == "application/json"


class TestBuildSession:
    def test_mounts_gitlab_adapter(self):
        session = build_session()
        adapter = session.get_adapter("https://gitlab.com/api/v4/projects")
        assert isinstance(adapter, GitLabHTTPAdapter)

    def test_mounts_github_adapter(self):
        session = build_session(github_token="tok")
        adapter = session.get_adapter("https://api.github.com/repos/o/r")
        assert isinstance(adapter, GitHubHTTPAdapter)

    def test_github_adapter_receives_token(self):
        session = build_session(github_token="my-gh-token")
        adapter = session.get_adapter("https://api.github.com/repos/o/r")
        assert adapter._token == "my-gh-token"


class TestExtractApiError:
    def test_message_string(self):
        resp = MagicMock()
        resp.json.return_value = {"message": "Not Found"}
        assert extract_api_error(resp) == "Not Found"

    def test_message_list(self):
        resp = MagicMock()
        resp.json.return_value = {"message": ["error one", "error two"]}
        assert extract_api_error(resp) == "error one; error two"

    def test_errors_array_dict(self):
        resp = MagicMock()
        resp.json.return_value = {"errors": [{"message": "field invalid"}]}
        assert extract_api_error(resp) == "field invalid"

    def test_errors_array_string(self):
        resp = MagicMock()
        resp.json.return_value = {"errors": ["something wrong"]}
        assert extract_api_error(resp) == "something wrong"

    def test_unknown_error_fallback(self):
        resp = MagicMock()
        resp.json.return_value = {}
        assert extract_api_error(resp) == "Unknown error"

    def test_json_decode_error(self):
        resp = MagicMock()
        resp.json.side_effect = ValueError("No JSON")
        assert extract_api_error(resp) == "Unknown error"
