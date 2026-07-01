"""MR/PR iteration lifecycle helpers.

Provides reusable functions for managing the iterate phase of MR/PR workflows:
extracting MR URLs from bot comments, counting iterations, checking readiness,
resolving review threads, and collecting feedback from forges.
"""

from __future__ import annotations

import logging
import re

import requests

from agentic_ci.forge import ForgeError, detect_forge

log = logging.getLogger(__name__)

DEFAULT_BOT_IDENTIFIER = "agentic-ci bot"


def extract_mr_url(
    comments: list[dict],
    bot_pattern: re.Pattern[str] | None = None,
) -> str | None:
    """Extract MR/PR URL from bot comments.

    Scans comments for those matching the bot header pattern, then extracts
    the first GitLab MR or GitHub PR URL found.

    Args:
        comments: List of comment dicts with ``"body"`` keys.
        bot_pattern: Compiled regex to identify bot comments.
            Defaults to matching ``jira-autofix bot`` headers.

    Returns:
        The MR/PR URL string, or ``None`` if not found.
    """
    if bot_pattern is None:
        bot_pattern = re.compile(
            rf"^(h3\.\s+|#{{1,6}}\s+)?{re.escape(DEFAULT_BOT_IDENTIFIER)}",
            re.MULTILINE,
        )
    for c in comments:
        body = c.get("body", "")
        if not bot_pattern.search(body):
            continue
        m = re.search(r"https?://gitlab\.com/\S+/-/merge_requests/\d+", body)
        if m:
            return m.group(0)
        m = re.search(r"https?://github\.com/\S+/pull/\d+", body)
        if m:
            return m.group(0)
    return None


def count_iterations(
    comments: list[dict],
    bot_identifier: str = DEFAULT_BOT_IDENTIFIER,
) -> int:
    """Count iteration comments posted by the bot.

    Looks for comments containing the bot identifier and an
    ``Iteration N/M:`` pattern.

    Args:
        comments: List of comment dicts with ``"body"`` keys.
        bot_identifier: String that identifies bot comments.

    Returns:
        Number of iteration comments found.
    """
    pattern = re.compile(r"Iteration \d+/\d+:")
    return sum(
        1
        for c in comments
        if bot_identifier in c.get("body", "") and pattern.search(c.get("body", ""))
    )


def already_notified_ready(
    comments: list[dict],
    bot_pattern: re.Pattern[str] | None = None,
) -> bool:
    """Check if last bot comment is a 'ready for merge' notification.

    Args:
        comments: List of comment dicts with ``"body"`` keys.
        bot_pattern: Compiled regex to identify bot comments.
            Defaults to matching ``jira-autofix bot`` headers.

    Returns:
        ``True`` if the last bot comment indicates the MR/PR is ready.
    """
    if bot_pattern is None:
        bot_pattern = re.compile(
            rf"^(h3\.\s+|#{{1,6}}\s+)?{re.escape(DEFAULT_BOT_IDENTIFIER)}",
            re.MULTILINE,
        )
    last_bot_body = ""
    for c in comments:
        if bot_pattern.search(c.get("body", "")):
            last_bot_body = c.get("body", "")
    return (
        "ready for a maintainer to review and merge" in last_bot_body
        or "ready for a maintainer to merge" in last_bot_body
    )


def resolve_threads(
    mr_url: str,
    review_comments: list[dict],
    reply_message: str = "Addressed in the latest revision.",
    github_token: str | None = None,
) -> int:
    """Reply to and resolve review threads on an MR/PR.

    For each comment with a ``thread_id``, posts a reply and resolves
    the thread via the detected forge API.

    Args:
        mr_url: Full URL of the MR/PR.
        review_comments: List of review comment dicts with ``"thread_id"`` keys.
        reply_message: Message to post as a reply before resolving.
        github_token: Token for GitHub API authentication.

    Returns:
        Number of threads successfully resolved.
    """
    if not review_comments:
        return 0
    try:
        forge = detect_forge(mr_url, github_token=github_token)
    except (ForgeError, requests.RequestException, ValueError, OSError) as exc:
        log.warning("forge detection error for %s: %s", mr_url, exc)
        return 0

    resolved = 0
    for comment in review_comments:
        thread_id = comment.get("thread_id")
        if not thread_id:
            continue
        try:
            forge.reply(mr_url, str(thread_id), reply_message)
            forge.resolve(mr_url, str(thread_id))
            resolved += 1
        except (ForgeError, requests.RequestException, ValueError, OSError) as exc:
            log.warning("forge thread resolve error: %s", exc)
    return resolved


def check_mr_state(
    mr_url: str,
    github_token: str | None = None,
) -> dict | None:
    """Check MR/PR state via the forge API.

    Args:
        mr_url: Full URL of the MR/PR.
        github_token: Token for GitHub API authentication.

    Returns:
        Dict with ``state``, ``source_branch``, ``pipeline_status`` keys,
        or ``None`` on error.
    """
    try:
        forge = detect_forge(mr_url, github_token=github_token)
        return forge.mr_status(mr_url)
    except (ForgeError, requests.RequestException, ValueError, OSError) as exc:
        log.warning("mr_status error for %s: %s", mr_url, exc)
        return None


def collect_feedback(
    mr_url: str,
    github_token: str | None = None,
) -> dict:
    """Collect review comments, general comments, and CI failures from an MR/PR.

    Makes three independent forge API calls, catching errors individually
    so partial results are still returned.

    Args:
        mr_url: Full URL of the MR/PR.
        github_token: Token for GitHub API authentication.

    Returns:
        Dict with keys: ``review_comments``, ``general_comments``,
        ``ci_failures``, ``review_count``, ``general_count``, ``ci_fail_count``.
    """
    empty: dict = {
        "review_comments": [],
        "general_comments": [],
        "ci_failures": {},
        "review_count": 0,
        "general_count": 0,
        "ci_fail_count": 0,
    }
    try:
        forge = detect_forge(mr_url, github_token=github_token)
    except (ForgeError, requests.RequestException, ValueError, OSError) as exc:
        log.warning("forge error for %s: %s", mr_url, exc)
        return empty

    review_comments: list[dict] = []
    general_comments: list[dict] = []
    ci_failures: dict = {}

    try:
        review_comments = forge.review_comments(mr_url) or []
    except (ForgeError, requests.RequestException, ValueError, OSError) as exc:
        log.warning("review_comments error for %s: %s", mr_url, exc)
    try:
        general_comments = forge.general_comments(mr_url) or []
    except (ForgeError, requests.RequestException, ValueError, OSError) as exc:
        log.warning("general_comments error for %s: %s", mr_url, exc)
    try:
        ci_failures = forge.pipeline_failures(mr_url) or {}
    except (ForgeError, requests.RequestException, ValueError, OSError) as exc:
        log.warning("pipeline_failures error for %s: %s", mr_url, exc)

    ci_fail_count = len(ci_failures.get("failed_jobs", [])) if isinstance(ci_failures, dict) else 0
    return {
        "review_comments": review_comments,
        "general_comments": general_comments,
        "ci_failures": ci_failures,
        "review_count": len(review_comments),
        "general_count": len(general_comments),
        "ci_fail_count": ci_fail_count,
    }
