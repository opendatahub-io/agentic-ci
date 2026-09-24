"""Git forge abstraction for GitLab and GitHub.

Provides a polymorphic interface for merge/pull request operations,
pipeline status checking, and review comment handling. Follows the
same ABC pattern as ``agentic_ci.backend`` and ``agentic_ci.harness``.

Usage::

    from agentic_ci.forge import Forge

    forge = Forge.detect("https://gitlab.com/org/repo/-/merge_requests/42")
    status = forge.mr_status("https://gitlab.com/org/repo/-/merge_requests/42")

Label helpers (callers choose names and create-vs-attach policy)::

    forge = Forge.detect(repo_url)
    if not forge.label_exists(repo_url, "autofix"):
        forge.create_label(repo_url, "autofix")
    forge.add_mr_labels(mr_url, ["autofix"])

Comment author trust (keep feedback only from repository owners, organization
members and collaborators on GitHub, or Developers and above on GitLab; the
GitHub check uses author_association, not repository permissions)::

    forge = Forge.detect(mr_url, github_token=token)
    threads = filter_trusted_threads(forge.review_comments(mr_url))
    comments = filter_trusted_comments(forge.general_comments(mr_url))

GitHub comments carry ``author_association`` and GitLab comments carry
``author_access_level``. Only ``TRUSTED_GITHUB_ASSOCIATIONS`` and levels
at or above ``MIN_TRUSTED_GITLAB_ACCESS_LEVEL`` (Developer) are trusted.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse

# Default hex color for newly created labels (no leading '#').
DEFAULT_LABEL_COLOR = "808080"


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
    def mr_status(self, mr_url: str, *, ignored_checks: frozenset[str] | None = None) -> dict:
        """Get MR/PR state, source branch, and pipeline status.

        Returns ``{"state": str, "source_branch": str, "pipeline_status": str}``.
        State is normalized to ``"open"``, ``"merged"``, or ``"closed"``.

        When *ignored_checks* is provided, check runs whose ``name`` (or
        commit statuses whose ``context``) is in the set are excluded
        before determining the overall pipeline status.
        """

    @abstractmethod
    def review_comments(self, mr_url: str) -> list[dict]:
        """Get unresolved review comment threads with diff positions.

        Returns a list of dicts with keys:
        ``thread_id``, ``file``, ``line``, ``body``, ``author``, ``comments``.

        ``body`` joins every comment as ``"<author>: <body>"`` lines and
        ``author`` is the thread starter. ``comments`` lists each comment
        of the thread as a dict with ``author``, ``body``, ``created_at``
        and the author's trust field: ``author_association`` on GitHub, or
        ``author_username`` and ``author_access_level`` on GitLab. The
        thread dict also carries the starter's trust field. Use
        :func:`filter_trusted_threads` to drop untrusted comments.
        """

    @abstractmethod
    def general_comments(
        self,
        mr_url: str,
        since: str | None = None,
        skip_patterns: list[str] | None = None,
    ) -> list[dict]:
        """Get general (non-diff-positioned) MR/PR comments.

        Returns a list of dicts with keys: ``author``, ``body``, ``created_at``,
        plus the author's trust field: ``author_association`` on GitHub, or
        ``author_username`` and ``author_access_level`` on GitLab. Use
        :func:`filter_trusted_comments` to drop untrusted comments.
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
    def label_exists(self, repo_url: str, label: str) -> bool:
        """Return whether ``label`` exists on the repository/project.

        Does not create the label. Returns ``False`` when the label is
        missing (including HTTP 404). Raises ``ForgeError`` on other
        API failures.
        """

    @abstractmethod
    def create_label(
        self,
        repo_url: str,
        label: str,
        *,
        color: str | None = None,
        description: str | None = None,
    ) -> None:
        """Create a repository/project label.

        Callers must opt in explicitly; attaching labels does not
        implicitly create them via this API. When ``color`` is omitted,
        ``DEFAULT_LABEL_COLOR`` is used.

        Raises ``ForgeError`` on API failure (including if the label
        already exists).
        """

    @abstractmethod
    def add_mr_labels(self, mr_url: str, labels: list[str]) -> None:
        """Attach labels to an existing MR/PR, preserving current labels.

        No-op when ``labels`` is empty. Does not check whether labels
        exist on the repository first — callers that need that policy
        should use :meth:`label_exists` / :meth:`create_label`.

        Raises ``ForgeError`` on API failure.
        """

    @abstractmethod
    def pipeline_failures(
        self, mr_url: str, *, ignored_checks: frozenset[str] | None = None
    ) -> dict:
        """Get failed CI job names and log tails.

        Returns ``{"pipeline_status": str, "failed_jobs": [{"name", "id", "log"}]}``.

        When *ignored_checks* is provided, those checks are excluded from
        both the pipeline status derivation and the ``failed_jobs`` list.
        An additional ``ignored_checks_status`` key reports the aggregate
        status of the ignored checks alone.
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


# GitHub ``author_association`` values trusted as MR/PR feedback: the
# repository owner, organization members and invited collaborators.
# CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, FIRST_TIMER, MANNEQUIN and NONE are
# untrusted, since any GitHub account can reach them on a public repository.
TRUSTED_GITHUB_ASSOCIATIONS: frozenset[str] = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# GitLab project access level of the Developer role.
GITLAB_DEVELOPER_ACCESS_LEVEL = 30

# Minimum GitLab project access level trusted as MR feedback (Developer).
# Guest (10), Planner (15), Reporter (20), non-members (0) and authors
# whose level could not be resolved (None) are untrusted.
MIN_TRUSTED_GITLAB_ACCESS_LEVEL = GITLAB_DEVELOPER_ACCESS_LEVEL


def is_trusted_comment(comment: dict) -> bool:
    """Return whether a forge comment was written by a trusted author.

    A GitHub comment (one with an ``author_association`` key) is trusted
    when the association is in ``TRUSTED_GITHUB_ASSOCIATIONS``. Any other
    comment is trusted only when its ``author_access_level`` is an integer
    at or above ``MIN_TRUSTED_GITLAB_ACCESS_LEVEL``. Comments carrying
    neither field are untrusted, so the check fails closed.
    """
    if "author_association" in comment:
        return comment["author_association"] in TRUSTED_GITHUB_ASSOCIATIONS
    level = comment.get("author_access_level")
    if isinstance(level, bool) or not isinstance(level, int):
        return False
    return level >= MIN_TRUSTED_GITLAB_ACCESS_LEVEL


def filter_trusted_comments(comments: list[dict]) -> list[dict]:
    """Return only the comments written by trusted authors.

    Takes the output of :meth:`Forge.general_comments` (or any list of
    comment dicts) and keeps those for which :func:`is_trusted_comment`
    holds.
    """
    return [c for c in comments if is_trusted_comment(c)]


def filter_trusted_threads(threads: list[dict]) -> list[dict]:
    """Return review threads reduced to their trusted comments.

    Takes the output of :meth:`Forge.review_comments`. Each thread's
    ``comments`` keeps only trusted comments and ``body`` is rebuilt from
    them, so an untrusted reply inside a trusted author's thread is
    dropped. A thread with no trusted comment left, or without a
    ``comments`` list, is dropped. The thread's position, ``author`` and
    trust fields still describe the thread starter. Input dicts are not
    modified.
    """
    result: list[dict] = []
    for thread in threads:
        trusted = filter_trusted_comments(thread.get("comments") or [])
        if not trusted:
            continue
        body = "\n".join(f"{c.get('author', 'Unknown')}: {c.get('body', '')}" for c in trusted)
        result.append({**thread, "comments": trusted, "body": body})
    return result


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
