"""Git forge abstraction for GitLab and GitHub.

Provides a polymorphic interface for merge/pull request operations,
pipeline status checking, and review comment handling. Follows the
same ABC pattern as ``agentic_ci.backend`` and ``agentic_ci.harness``.

Usage::

    from agentic_ci.forge import Forge

    forge = Forge.detect("https://gitlab.com/org/repo/-/merge_requests/42")
    status = forge.mr_status("https://gitlab.com/org/repo/-/merge_requests/42")
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse


class ForgeError(Exception):
    """Raised when a forge API operation fails."""


class Forge(ABC):
    """Abstract base for git forge (GitLab/GitHub) API operations.

    Concrete implementations handle authentication and API differences.
    Use ``Forge.detect(url)`` to get the right implementation for a URL.
    """

    @classmethod
    def detect(cls, url: str, *, github_token: str | None = None) -> Forge:
        """Return the correct ``Forge`` implementation for a URL.

        Inspects the hostname to choose between GitLab and GitHub.

        Args:
            url: Any URL on the forge (repo URL, MR/PR URL, etc.).
            github_token: Token for GitHub API authentication.

        Raises:
            ForgeError: If the URL hostname is not recognized.
        """
        parsed = urlparse(url)
        if parsed.hostname == "gitlab.com":
            from agentic_ci.forge.gitlab import GitLabForge

            return GitLabForge()
        if parsed.hostname == "github.com":
            from agentic_ci.forge.github import GitHubForge

            return GitHubForge(token=github_token)
        raise ForgeError(f"Unrecognized forge host: {parsed.hostname} (URL: {url})")

    @abstractmethod
    def create_merge_request(
        self,
        repo_url: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        draft: bool = False,
    ) -> tuple[str | None, str | None]:
        """Create an MR/PR.

        Returns ``(web_url, None)`` on success or ``(None, error_msg)``
        on failure.
        """

    @abstractmethod
    def mr_status(self, mr_url: str) -> dict:
        """Get MR/PR state, source branch, and pipeline status.

        Returns ``{"state": str, "source_branch": str, "pipeline_status": str}``.
        State is normalized to ``"open"``, ``"merged"``, or ``"closed"``.
        """

    @abstractmethod
    def review_comments(self, mr_url: str) -> list[dict]:
        """Get unresolved review comment threads with diff positions.

        Returns a list of dicts with keys:
        ``thread_id``, ``file``, ``line``, ``body``, ``author``.
        """

    @abstractmethod
    def general_comments(
        self,
        mr_url: str,
        since: str | None = None,
        skip_patterns: list[str] | None = None,
    ) -> list[dict]:
        """Get general (non-diff-positioned) MR/PR comments.

        Returns a list of dicts with keys: ``author``, ``body``, ``created_at``.
        Comments created before ``since`` (ISO 8601) are excluded.
        Comments containing any string in ``skip_patterns`` are excluded.
        If ``skip_patterns`` is None, a default list is used.
        """

    @abstractmethod
    def reply(self, mr_url: str, thread_id: str, message: str) -> None:
        """Reply to a review comment thread."""

    @abstractmethod
    def resolve(self, mr_url: str, thread_id: str) -> None:
        """Resolve a review comment thread."""

    @abstractmethod
    def update_description(
        self,
        mr_url: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        """Update an existing MR/PR title and/or description.

        Only the provided keyword arguments are updated; omitted fields
        are left unchanged.

        Raises ``ForgeError`` on API failure.
        """

    @abstractmethod
    def pipeline_failures(self, mr_url: str) -> dict:
        """Get failed CI job names and log tails.

        Returns ``{"pipeline_status": str, "failed_jobs": [{"name", "id", "log"}]}``.
        """


_GITLAB_MR_RE = re.compile(
    r"https?://gitlab\.com/(.+?)/-/merge_requests/(\d+)",
)
_GITHUB_PR_RE = re.compile(
    r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)",
)

DEFAULT_SKIP_PATTERNS: list[str] = [
    "<!-- agentic-ci",
    "<!-- ai-review",
    "Addressed in the latest revision",
]


def parse_gitlab_mr_url(url: str) -> tuple[str, int]:
    """Parse a GitLab MR URL into ``(project_path, mr_iid)``.

    Raises ``ForgeError`` if the URL does not match the expected pattern.
    """
    match = _GITLAB_MR_RE.match(url)
    if not match:
        raise ForgeError(f"Invalid GitLab MR URL: {url}")
    return match.group(1), int(match.group(2))


def parse_github_pr_url(url: str) -> tuple[str, int]:
    """Parse a GitHub PR URL into ``(owner/repo, pr_number)``.

    Raises ``ForgeError`` if the URL does not match the expected pattern.
    """
    match = _GITHUB_PR_RE.match(url)
    if not match:
        raise ForgeError(f"Invalid GitHub PR URL: {url}")
    return match.group(1), int(match.group(2))


def repo_path_from_url(url: str) -> str:
    """Extract the repository path from a GitLab or GitHub URL.

    Strips trailing slashes and ``.git`` suffixes.
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


def detect_forge(url: str, *, github_token: str | None = None) -> Forge:
    """Convenience wrapper around ``Forge.detect()``."""
    return Forge.detect(url, github_token=github_token)
