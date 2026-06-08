"""GitHub forge implementation.

Provides ``GitHubForge`` for interacting with the GitHub REST and
GraphQL APIs (pull requests, check runs, review threads).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests

from agentic_ci.forge import (
    DEFAULT_SKIP_PATTERNS,
    Forge,
    ForgeError,
    parse_github_pr_url,
    repo_path_from_url,
)
from agentic_ci.forge.session import API_TIMEOUT, build_session, extract_api_error

log = logging.getLogger(__name__)

_GITHUB_ORG_RE = re.compile(r"github\.com/([^/]+)", re.IGNORECASE)


class GitHubForge(Forge):
    """GitHub REST + GraphQL API implementation of the ``Forge`` interface."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._session = build_session(github_token=token)

    def create_merge_request(
        self,
        repo_url: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> tuple[str | None, str | None]:
        repo_path = repo_path_from_url(repo_url)
        payload = {
            "head": source_branch,
            "base": target_branch,
            "title": title,
            "body": description,
        }
        resp = self._session.post(
            f"https://api.github.com/repos/{repo_path}/pulls",
            json=payload,
        )
        if resp.status_code not in (200, 201):
            error = extract_api_error(resp)
            log.error("HTTP %d creating PR: %s", resp.status_code, error)
            return None, error
        return resp.json().get("html_url"), None

    def mr_status(self, mr_url: str) -> dict:
        repo_path, pr_number = parse_github_pr_url(mr_url)
        resp = self._session.get(
            f"https://api.github.com/repos/{repo_path}/pulls/{pr_number}",
        )
        if resp.status_code != 200:
            raise ForgeError(f"HTTP {resp.status_code}: {resp.text}")
        pr = resp.json()
        if pr.get("merged"):
            state = "merged"
        else:
            state = pr.get("state", "unknown")
        head_sha = pr.get("head", {}).get("sha", "")
        pipeline_status = "unknown"
        if head_sha:
            check_runs, accessible = self.check_runs(repo_path, head_sha)
            statuses = self.commit_statuses(repo_path, head_sha)
            pipeline_status = _derive_pipeline_status(
                check_runs,
                accessible,
                commit_statuses=statuses,
            )
        return {
            "state": state,
            "source_branch": pr.get("head", {}).get("ref", ""),
            "pipeline_status": pipeline_status,
        }

    def update_description(
        self,
        mr_url: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        repo_path, pr_number = parse_github_pr_url(mr_url)
        payload: dict[str, str] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["body"] = description
        if not payload:
            return
        resp = self._session.patch(
            f"https://api.github.com/repos/{repo_path}/pulls/{pr_number}",
            json=payload,
        )
        if resp.status_code != 200:
            error = extract_api_error(resp)
            raise ForgeError(f"HTTP {resp.status_code} updating PR: {error}")

    def review_comments(self, mr_url: str) -> list[dict]:
        repo_path, pr_number = parse_github_pr_url(mr_url)
        owner, repo = repo_path.split("/", 1)
        query = """
        query($owner: String!, $repo: String!, $pr: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $pr) {
              reviewThreads(first: 100) {
                nodes {
                  id
                  isResolved
                  comments(first: 50) {
                    nodes {
                      body
                      path
                      line
                      author { login }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"owner": owner, "repo": repo, "pr": pr_number})
        threads = []
        review_threads = (
            data.get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        for thread in review_threads:
            if thread.get("isResolved", True):
                continue
            comments = thread.get("comments", {}).get("nodes", [])
            if not comments:
                continue
            first = comments[0]
            body_parts = []
            for c in comments:
                author = c.get("author", {}).get("login", "Unknown")
                body_parts.append(f"{author}: {c.get('body', '')}")
            threads.append(
                {
                    "thread_id": thread["id"],
                    "file": first.get("path", ""),
                    "line": first.get("line") or 0,
                    "body": "\n".join(body_parts),
                    "author": first.get("author", {}).get("login", "Unknown"),
                }
            )
        return threads

    def general_comments(
        self,
        mr_url: str,
        since: str | None = None,
        skip_patterns: list[str] | None = None,
    ) -> list[dict]:
        if skip_patterns is None:
            skip_patterns = DEFAULT_SKIP_PATTERNS
        repo_path, pr_number = parse_github_pr_url(mr_url)
        params: dict = {"per_page": 100}
        if since:
            params["since"] = since
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        comments = []
        page = 1
        while True:
            params["page"] = page
            resp = self._session.get(
                f"https://api.github.com/repos/{repo_path}/issues/{pr_number}/comments",
                params=params,
            )
            if resp.status_code != 200:
                raise ForgeError(f"HTTP {resp.status_code}: {resp.text}")
            batch = resp.json()
            if not batch:
                break
            for c in batch:
                body = c.get("body", "")
                if any(pat in body for pat in skip_patterns):
                    continue
                if since_dt:
                    created = c.get("created_at", "")
                    if created:
                        try:
                            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            if created_dt < since_dt:
                                continue
                        except (ValueError, TypeError):
                            pass
                comments.append(
                    {
                        "author": c.get("user", {}).get("login", "Unknown"),
                        "body": body,
                        "created_at": c.get("created_at", ""),
                    }
                )
            if len(batch) < 100:
                break
            page += 1
        return comments

    def reply(self, mr_url: str, thread_id: str, message: str) -> None:
        mutation = """
        mutation($threadId: ID!, $body: String!) {
          addPullRequestReviewThreadReply(
            input: {pullRequestReviewThreadId: $threadId, body: $body}
          ) {
            comment { id }
          }
        }
        """
        self.graphql(mutation, {"threadId": thread_id, "body": message})

    def resolve(self, mr_url: str, thread_id: str) -> None:
        mutation = """
        mutation($threadId: ID!) {
          resolveReviewThread(input: {threadId: $threadId}) {
            thread { isResolved }
          }
        }
        """
        self.graphql(mutation, {"threadId": thread_id})

    def pipeline_failures(self, mr_url: str) -> dict:
        repo_path, pr_number = parse_github_pr_url(mr_url)
        resp = self._session.get(
            f"https://api.github.com/repos/{repo_path}/pulls/{pr_number}",
        )
        if resp.status_code != 200:
            raise ForgeError(f"HTTP {resp.status_code}: {resp.text}")
        pr = resp.json()
        head_sha = pr.get("head", {}).get("sha", "")
        if not head_sha:
            return {"pipeline_status": "none", "failed_jobs": []}
        check_runs, accessible = self.check_runs(repo_path, head_sha)
        statuses = self.commit_statuses(repo_path, head_sha)
        pipeline_status = _derive_pipeline_status(
            check_runs,
            accessible,
            commit_statuses=statuses,
        )
        if pipeline_status in ("success", "none", "running", "unknown"):
            return {"pipeline_status": pipeline_status, "failed_jobs": []}
        failed_jobs = []
        for s in statuses:
            if _STATUS_STATE_MAP.get(s.get("state", "")) == "failure":
                context = s.get("context", "unknown")
                target_url = s.get("target_url", "")
                description = s.get("description", "")
                log_text = f"Status: {s.get('state')}\nContext: {context}"
                if description:
                    log_text += f"\nDescription: {description}"
                if target_url:
                    log_text += f"\nDetails: {target_url}"
                failed_jobs.append({"name": context, "id": s.get("id", 0), "log": log_text})
        for cr in check_runs:
            if cr.get("status") != "completed":
                continue
            if cr.get("conclusion") not in ("failure", "timed_out", "startup_failure"):
                continue
            job_name = cr.get("name", "unknown")
            check_run_id = cr.get("id")
            log_text = ""
            log_resp = self._session.get(
                f"https://api.github.com/repos/{repo_path}/actions/jobs/{check_run_id}/logs",
            )
            if log_resp.status_code == 200:
                lines = log_resp.text.splitlines()
                log_text = "\n".join(lines[-200:])
            else:
                output = cr.get("output", {}) or {}
                text = output.get("text") or output.get("summary") or ""
                if text:
                    lines = text.splitlines()
                    log_text = "\n".join(lines[-200:])
            failed_jobs.append({"name": job_name, "id": check_run_id, "log": log_text})
        return {"pipeline_status": pipeline_status, "failed_jobs": failed_jobs}

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GitHub GraphQL query or mutation.

        Returns the ``data`` dict from the response.
        Raises ``ForgeError`` on HTTP or GraphQL errors.
        """
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = self._session.post("https://api.github.com/graphql", json=payload)
        if resp.status_code != 200:
            raise ForgeError(f"GraphQL HTTP {resp.status_code}: {resp.text}")
        data = resp.json()
        if "errors" in data:
            raise ForgeError(f"GraphQL errors: {json.dumps(data['errors'])}")
        return data["data"]

    def check_runs(self, repo_path: str, sha: str) -> tuple[list[dict], bool]:
        """Fetch all check runs for a commit SHA.

        Returns ``(check_runs, accessible)`` where ``accessible`` is
        False when the token lacks ``checks:read`` permission (403).
        """
        all_runs: list[dict] = []
        page = 1
        while True:
            resp = self._session.get(
                f"https://api.github.com/repos/{repo_path}/commits/{sha}/check-runs",
                params={"per_page": 100, "page": page},
            )
            if resp.status_code == 403:
                log.warning("checks:read permission not available: %s", resp.text)
                return [], False
            if resp.status_code != 200:
                raise ForgeError(f"HTTP {resp.status_code} fetching check runs: {resp.text}")
            runs = resp.json().get("check_runs", [])
            all_runs.extend(runs)
            if len(runs) < 100:
                break
            page += 1
        return all_runs, True

    def commit_statuses(self, repo_path: str, sha: str) -> list[dict]:
        """Fetch commit statuses for a SHA.

        GitHub has two parallel CI reporting mechanisms: Check Runs
        (used by GitHub Actions) and Commit Statuses (the older API
        used by external CI like Prow, Jenkins, and other integrations).
        ``check_runs()`` only covers the first; this method covers the
        second so ``_derive_pipeline_status()`` sees the full picture.

        Merge-management contexts (``tide``, ``Mergify``) are excluded
        since they reflect merge policy, not CI results.

        See https://docs.github.com/en/rest/commits/statuses
        """
        all_statuses: list[dict] = []
        page = 1
        while True:
            resp = self._session.get(
                f"https://api.github.com/repos/{repo_path}/commits/{sha}/statuses",
                params={"per_page": 100, "page": page},
            )
            if resp.status_code == 403:
                log.debug("statuses not accessible for %s: %s", sha, resp.text)
                return []
            if resp.status_code != 200:
                log.warning("HTTP %d fetching commit statuses: %s", resp.status_code, resp.text)
                return []
            batch = resp.json()
            if not batch:
                break
            all_statuses.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        seen: dict[str, dict] = {}
        for s in all_statuses:
            ctx = s.get("context", "")
            if ctx not in seen:
                seen[ctx] = s
        return [s for s in seen.values() if not _is_merge_management_status(s.get("context", ""))]


_FAILED_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "startup_failure",
        "cancelled",
        "action_required",
    }
)
_PASSED_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

_MERGE_MGMT_PATTERNS = re.compile(
    r"^(tide$|Mergify)",
    re.IGNORECASE,
)


def _is_merge_management_status(context: str) -> bool:
    """Return True for merge-management contexts (tide, Mergify)."""
    return bool(_MERGE_MGMT_PATTERNS.search(context))


_STATUS_STATE_MAP = {"success": "success", "failure": "failure", "error": "failure"}


def _derive_pipeline_status(
    check_runs: list[dict],
    accessible: bool = True,
    commit_statuses: list[dict] | None = None,
) -> str:
    """Derive overall pipeline status from check runs and commit statuses."""
    if not accessible:
        return "unknown"
    if not check_runs and not commit_statuses:
        return "none"
    for cr in check_runs:
        if cr.get("status") != "completed":
            return "running"
    for s in commit_statuses or []:
        if s.get("state") == "pending":
            return "running"
    for cr in check_runs:
        if cr.get("conclusion") in _FAILED_CONCLUSIONS:
            return "failed"
    for s in commit_statuses or []:
        if _STATUS_STATE_MAP.get(s.get("state", "")) == "failure":
            return "failed"
    all_passed = (
        all(cr.get("conclusion") in _PASSED_CONCLUSIONS for cr in check_runs)
        if check_runs
        else True
    )
    statuses_passed = (
        all(s.get("state") == "success" for s in (commit_statuses or []))
        if commit_statuses
        else True
    )
    if all_passed and statuses_passed:
        return "success"
    return "unknown"


def generate_github_jwt(app_id: str | int, private_key_pem: str) -> str:
    """Generate a GitHub App JWT signed with RS256.

    The JWT is valid for 10 minutes (GitHub maximum).

    Requires the ``PyJWT`` and ``cryptography`` packages.
    Install with: ``pip install agentic-ci[forge]``
    """
    import jwt  # type: ignore[import-not-found]

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": str(app_id),
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def get_installation_token(jwt_token: str, installation_id: str | int) -> str:
    """Exchange a GitHub App JWT for an installation access token.

    Returns the token string (valid for 1 hour).
    Raises ``RuntimeError`` on failure.
    """
    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=API_TIMEOUT,
    )
    if resp.status_code != 201:
        raise RuntimeError(f"HTTP {resp.status_code} creating installation token: {resp.text}")
    return resp.json()["token"]


def _find_private_key(filename: str) -> Path | None:
    """Locate a GitHub App private key file.

    Searches ``SECURE_FILES_DOWNLOAD_PATH`` (defaulting to
    ``.secure_files``) then the current directory.
    """
    secure_dir = os.environ.get("SECURE_FILES_DOWNLOAD_PATH", ".secure_files")
    for path in [Path(secure_dir) / filename, Path(filename)]:
        if path.is_file():
            return path
    return None


def resolve_app_token(repo_url: str, github_config: dict) -> str | None:
    """Resolve a GitHub App installation token for a repo URL.

    Extracts the GitHub org from ``repo_url``, looks up the matching
    App configuration in ``github_config``, and returns a short-lived
    installation token.

    Returns ``None`` (with an error log) on any failure.
    """
    match = _GITHUB_ORG_RE.search(repo_url)
    if not match:
        log.error("Cannot extract GitHub org from URL: %s", repo_url)
        return None
    org_name = match.group(1).lower()
    app_config = None
    for key, value in github_config.items():
        if key.lower() == org_name:
            app_config = value
            break
    if not app_config:
        log.error("No GitHub App configured for org '%s'", org_name)
        return None
    credentials_env = app_config.get("credentials_env", "")
    private_key_file = app_config.get("private_key_file", "")
    if not credentials_env or not private_key_file:
        log.error("Incomplete GitHub App config for org '%s'", org_name)
        return None
    credentials_json = os.environ.get(credentials_env, "")
    if not credentials_json:
        log.error("GitHub App env var %s is not set", credentials_env)
        return None
    try:
        creds = json.loads(credentials_json)
    except (json.JSONDecodeError, TypeError):
        log.error("GitHub App env var %s is not valid JSON", credentials_env)
        return None
    app_id = creds.get("app_id", "")
    installation_id = creds.get("installation_id", "")
    if not app_id or not installation_id:
        log.error("GitHub App credentials missing app_id or installation_id")
        return None
    key_path = _find_private_key(private_key_file)
    if not key_path:
        log.error("GitHub App private key file not found: %s", private_key_file)
        return None
    try:
        private_key_pem = key_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Cannot read private key %s: %s", key_path, exc)
        return None
    try:
        jwt_token = generate_github_jwt(app_id, private_key_pem)
        return get_installation_token(jwt_token, installation_id)
    except Exception as exc:
        log.error("GitHub token generation failed: %s", exc)
        return None
