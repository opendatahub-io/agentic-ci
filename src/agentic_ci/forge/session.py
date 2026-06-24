"""HTTP session and adapter configuration for forge API calls.

Provides auth-injecting adapters for GitLab (PRIVATE-TOKEN) and
GitHub (Bearer token), plus a pre-configured session with retry logic.
"""

from __future__ import annotations

import logging
import os

import requests
import tenacity
from requests.adapters import HTTPAdapter

_log = logging.getLogger(__name__)

try:
    API_TIMEOUT = int(os.getenv("FORGE_API_TIMEOUT", "30"))
except (ValueError, TypeError):
    _log.warning("FORGE_API_TIMEOUT is not a valid integer, using default 30")
    API_TIMEOUT = 30

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRYABLE_STATUSES
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


_forge_retry = tenacity.retry(
    retry=tenacity.retry_if_exception(_is_retryable),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=60),
    stop=tenacity.stop_after_attempt(5),
    reraise=True,
)


class ForgeAuthError(RuntimeError):
    """Raised when forge API authentication credentials are missing."""


class GitLabHTTPAdapter(HTTPAdapter):
    """Requests adapter that injects ``PRIVATE-TOKEN`` for GitLab REST API."""

    def add_headers(self, request, **kwargs):
        token = os.getenv("BOT_PAT")
        if not token:
            raise ForgeAuthError("BOT_PAT environment variable not set")
        request.headers["PRIVATE-TOKEN"] = token

    @_forge_retry
    def send(self, request, timeout=None, **kwargs):
        if timeout is None:
            timeout = API_TIMEOUT
        resp = super().send(request, timeout=timeout, **kwargs)
        if resp.status_code in _RETRYABLE_STATUSES:
            resp.raise_for_status()
        return resp


class GitHubHTTPAdapter(HTTPAdapter):
    """Requests adapter that injects ``Bearer`` token for GitHub API."""

    def __init__(self, token: str | None, **kwargs):
        self._token = token
        super().__init__(**kwargs)

    def add_headers(self, request, **kwargs):
        if not self._token:
            raise ForgeAuthError("GitHub token is required for GitHub API operations")
        request.headers["Authorization"] = f"Bearer {self._token}"
        request.headers.setdefault("Accept", "application/vnd.github+json")

    @_forge_retry
    def send(self, request, timeout=None, **kwargs):
        if timeout is None:
            timeout = API_TIMEOUT
        resp = super().send(request, timeout=timeout, **kwargs)
        if resp.status_code in _RETRYABLE_STATUSES:
            resp.raise_for_status()
        return resp


def build_session(
    *,
    gitlab_adapter: GitLabHTTPAdapter | None = None,
    github_token: str | None = None,
) -> requests.Session:
    """Build a requests session with forge-specific auth adapters.

    The session automatically injects the correct auth headers based
    on the request URL prefix (``gitlab.com`` or ``api.github.com``).

    Args:
        gitlab_adapter: Custom GitLab HTTP adapter. Defaults to
            ``GitLabHTTPAdapter()`` which uses ``BOT_PAT``.
        github_token: Token for GitHub API authentication.
    """
    s = requests.Session()
    s.mount("https://gitlab.com", gitlab_adapter or GitLabHTTPAdapter())
    s.mount(
        "https://api.github.com",
        GitHubHTTPAdapter(token=github_token),
    )
    return s


def extract_api_error(resp: requests.Response) -> str:
    """Extract a human-readable error message from a forge API error response.

    Tries ``message``, then ``errors[0].message``, falling back to
    ``"Unknown error"``.
    """
    try:
        data = resp.json()
        msg = data.get("message") or ""
        if isinstance(msg, list):
            msg = "; ".join(str(m) for m in msg)
        if not msg:
            errors = data.get("errors", [])
            if errors and isinstance(errors[0], dict):
                msg = errors[0].get("message", "")
            elif errors:
                msg = str(errors[0])
        return msg or "Unknown error"
    except (KeyError, TypeError, AttributeError, ValueError):
        return "Unknown error"
