"""Jira client with acli-first delegation and REST API fallback.

Provides a ``JiraClient`` class that delegates to the Atlassian CLI
(``acli``) for operations it supports and falls back to the REST API
for gaps (changelog queries, visibility-restricted comments, custom
fields, attachment upload, ADF conversion).

When ``acli`` is not on PATH, all operations use the REST API.

Usage::

    from agentic_ci.jira import JiraClient

    client = JiraClient.from_env(url="https://myorg.atlassian.net")
    ticket = client.get_issue("PROJ-123")
    client.add_comment("PROJ-123", "Fixed in PR #42")
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
from pathlib import Path
from typing import Any

import requests
import tenacity
from requests.exceptions import HTTPError

from agentic_ci.jira import acli as acli_mod
from agentic_ci.jira.adf import adf_to_text, text_to_adf

log = logging.getLogger(__name__)

API_VERSION = "3"
MAX_RETRY_AFTER = 60


def _is_rate_limit_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, HTTPError) and exc.response is not None and exc.response.status_code == 429
    )


def _wait_rate_limit(retry_state: tenacity.RetryCallState) -> float:
    backoff = min(2 ** (retry_state.attempt_number - 1), MAX_RETRY_AFTER)
    delay = backoff

    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, HTTPError) and exc.response is not None:
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                parsed = float(retry_after)
                if math.isfinite(parsed) and parsed > 0:
                    delay = max(parsed, backoff)
            except (ValueError, OverflowError):
                pass

    delay = min(delay, MAX_RETRY_AFTER)
    return delay + random.uniform(0, delay * 0.25)


def _return_last_response(retry_state: tenacity.RetryCallState) -> requests.Response:
    exc = retry_state.outcome.exception()  # type: ignore[union-attr]
    if isinstance(exc, HTTPError) and exc.response is not None:
        return exc.response
    raise exc  # type: ignore[misc]


class JiraError(Exception):
    """Raised when a Jira API call fails."""

    def __init__(self, message: str, status_code: int | None = None, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class JiraClient:
    """Jira client that delegates to acli where possible.

    On init, checks for ``acli`` on PATH. If available, write
    operations (create, edit, transition, assign, comment, link)
    and search/view use acli subprocess calls. Read operations
    that need ADF conversion, changelog queries, custom fields,
    or visibility-restricted comments fall back to the REST API.
    """

    def __init__(self, url: str, email: str, token: str, *, timeout: int = 30):
        self.url = url.rstrip("/")
        self.auth = (email, token)
        self.timeout = timeout
        self._field_cache: dict[str, str] | None = None
        self._field_schema_cache: dict[str, str] | None = None
        self._acli_available = acli_mod.is_available()
        if self._acli_available:
            log.debug("acli detected on PATH, will delegate supported operations")

    @classmethod
    def from_env(cls, url: str | None = None) -> JiraClient:
        """Create a client from environment variables.

        Reads ``JIRA_URL``, ``JIRA_EMAIL`` (or ``JIRA_USER``), and
        ``JIRA_API_TOKEN``.

        Raises ``RuntimeError`` if required variables are missing.
        """
        jira_url = url or os.environ.get("JIRA_URL", "")
        if not jira_url:
            raise RuntimeError("JIRA_URL environment variable not set and no url provided")
        email = os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USER", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        missing = []
        if not email:
            missing.append("JIRA_EMAIL (or JIRA_USER)")
        if not token:
            missing.append("JIRA_API_TOKEN")
        if missing:
            raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")
        timeout = int(os.environ.get("JIRA_API_TIMEOUT", "30"))
        return cls(jira_url, email, token, timeout=timeout)

    def _api_url(self, path: str) -> str:
        return f"{self.url}/rest/api/{API_VERSION}/{path}"

    def _headers(self, *, json_content: bool = True) -> dict[str, str]:
        h: dict[str, str] = {}
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    def _check(self, resp: requests.Response, *, expected: int | tuple[int, ...] = 200) -> None:
        if isinstance(expected, int):
            expected = (expected,)
        if resp.status_code not in expected:
            raise JiraError(
                f"Jira API error: HTTP {resp.status_code}",
                status_code=resp.status_code,
                response_text=resp.text,
            )

    @tenacity.retry(
        retry=tenacity.retry_if_exception(_is_rate_limit_error),
        wait=_wait_rate_limit,
        stop=tenacity.stop_after_attempt(5),
        before_sleep=lambda rs: log.warning(
            "Jira API rate limited (429), retrying in %.1fs (attempt %d)",
            rs.idle_for,
            rs.attempt_number,
        ),
        retry_error_callback=_return_last_response,
    )
    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("auth", self.auth)
        resp = getattr(requests, method)(url, **kwargs)
        if resp.status_code == 429:
            raise HTTPError("429 Too Many Requests", response=resp)
        return resp

    # ------------------------------------------------------------------
    # Field metadata
    # ------------------------------------------------------------------

    def _load_field_metadata(self) -> None:
        """Fetch and cache field metadata (id + schema type) from the Jira API."""
        if self._field_cache is not None:
            return
        resp = self._request(
            "get",
            self._api_url("field"),
            headers=self._headers(),
        )
        self._check(resp)
        self._field_cache = {}
        self._field_schema_cache = {}
        for f in resp.json():
            name = f.get("name", "")
            fid = f.get("id", "")
            self._field_cache[name] = fid
            self._field_schema_cache[name] = f.get("schema", {}).get("type", "")
            self._field_schema_cache[fid] = f.get("schema", {}).get("type", "")

    def _resolve_account_id(self, assignee: str) -> str:
        """Resolve an email address to a Jira account ID.

        If the input is already an account ID (no '@'), returns it as-is.
        """
        assignee = assignee.strip()
        if not assignee:
            raise JiraError("Assignee cannot be empty")

        if "@" not in assignee:
            return assignee

        resp = self._request(
            "get",
            self._api_url("user/search"),
            headers=self._headers(),
            params={"query": assignee},
        )
        self._check(resp)
        users = resp.json()
        if not users:
            raise JiraError(f"No Jira user found for '{assignee}'")
        account_id = users[0].get("accountId")
        if not account_id:
            raise JiraError(f"Jira user search returned invalid response for '{assignee}'")
        return account_id

    def _resolve_field_id(self, field_name: str) -> str:
        """Resolve a human-readable field name to its ``customfield_XXXXX`` ID."""
        self._load_field_metadata()
        assert self._field_cache is not None
        fid = self._field_cache.get(field_name)
        if not fid:
            raise JiraError(f"Field '{field_name}' not found in Jira metadata")
        return fid

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_issue(self, key: str) -> dict:
        """Fetch a single issue with comments. Returns a normalised dict.

        The returned dict has keys: ``key``, ``summary``, ``description``
        (plain text), ``issue_type``, ``labels``, ``status``, ``project``,
        ``components``, ``reporter_name``, ``reporter_email``, ``comments``.
        """
        req_fields = "summary,description,issuetype,labels,status,reporter,components,project"
        resp = self._request(
            "get",
            self._api_url(f"issue/{key}") + f"?fields={req_fields}",
            headers=self._headers(),
        )
        self._check(resp)
        issue = resp.json()
        fields = issue.get("fields", {})

        desc_field = fields.get("description")
        if isinstance(desc_field, dict):
            description = adf_to_text(desc_field)
        else:
            description = desc_field or ""

        comments = self._fetch_comments(key)
        reporter = fields.get("reporter") or {}
        components = [{"name": c.get("name", "")} for c in fields.get("components", [])]

        return {
            "key": issue.get("key", ""),
            "summary": fields.get("summary", ""),
            "description": description,
            "issue_type": fields.get("issuetype", {}).get("name", ""),
            "labels": fields.get("labels", []),
            "status": fields.get("status", {}).get("name", ""),
            "project": {"key": fields.get("project", {}).get("key", "")},
            "components": components,
            "reporter_name": reporter.get("displayName", ""),
            "reporter_email": reporter.get("emailAddress", ""),
            "comments": comments,
        }

    def _fetch_comments(self, key: str) -> list[dict]:
        resp = self._request(
            "get",
            self._api_url(f"issue/{key}/comment"),
            headers=self._headers(),
        )
        self._check(resp)
        comments = []
        for c in resp.json().get("comments", []):
            body_field = c.get("body", "")
            body = adf_to_text(body_field) if isinstance(body_field, dict) else body_field
            comments.append(
                {
                    "author": c.get("author", {}).get("displayName", "Unknown"),
                    "author_email": c.get("author", {}).get("emailAddress", ""),
                    "body": body,
                    "created": c.get("created", ""),
                    "visibility": c.get("visibility"),
                }
            )
        return comments

    def search(self, jql: str, *, max_results: int = 500) -> list[dict]:
        """Search issues by JQL. Returns a list of normalised dicts."""
        results: list[dict] = []
        next_page_token: str | None = None
        search_url = self._api_url("search/jql")

        while True:
            payload: dict = {
                "jql": jql,
                "fields": ["summary", "description", "issuetype", "labels", "comment", "status"],
                "maxResults": min(50, max_results - len(results)),
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            resp = self._request(
                "post",
                search_url,
                headers=self._headers(),
                json=payload,
            )
            self._check(resp)

            data = resp.json()
            for issue in data.get("issues", []):
                fields = issue.get("fields", {})
                desc_field = fields.get("description")
                if isinstance(desc_field, dict):
                    description = adf_to_text(desc_field)
                else:
                    description = desc_field or ""

                comments_data = fields.get("comment", {})
                comments = []
                for c in comments_data.get("comments", []):
                    body_field = c.get("body", "")
                    body = adf_to_text(body_field) if isinstance(body_field, dict) else body_field
                    comments.append(
                        {
                            "author": c.get("author", {}).get("displayName", "Unknown"),
                            "author_email": c.get("author", {}).get("emailAddress", ""),
                            "body": body,
                            "created": c.get("created", ""),
                            "visibility": c.get("visibility"),
                        }
                    )

                results.append(
                    {
                        "key": issue.get("key", ""),
                        "summary": fields.get("summary", ""),
                        "description": description,
                        "issue_type": fields.get("issuetype", {}).get("name", ""),
                        "labels": fields.get("labels", []),
                        "status": fields.get("status", {}).get("name", ""),
                        "comments": comments,
                    }
                )

            if len(results) >= max_results:
                break
            if data.get("isLast", True) or not data.get("issues"):
                break
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return results

    def get_label_author(self, key: str, label: str) -> dict:
        """Find who most recently added a label via the changelog.

        Returns ``{"found": True, "email": ..., "displayName": ...}``
        or ``{"found": False}``.

        Falls back to the ticket reporter if the label was set at
        creation time (no changelog entry).
        """
        author_email: str | None = None
        author_name: str | None = None
        start_at = 0

        while True:
            resp = self._request(
                "get",
                self._api_url(f"issue/{key}/changelog"),
                headers=self._headers(),
                params={"startAt": start_at, "maxResults": 100},
            )
            self._check(resp)

            data = resp.json()
            for entry in data.get("values", []):
                for item in entry.get("items", []):
                    if item.get("field") != "labels":
                        continue
                    from_str = item.get("fromString") or ""
                    to_str = item.get("toString") or ""
                    from_labels = set(from_str.split()) if from_str else set()
                    to_labels = set(to_str.split()) if to_str else set()
                    if label in to_labels and label not in from_labels:
                        author = entry.get("author", {})
                        author_email = author.get("emailAddress", "")
                        author_name = author.get("displayName", "Unknown")

            total = data.get("total", 0)
            values = data.get("values", [])
            start_at += len(values)
            if start_at >= total or not values:
                break

        if author_email is None:
            resp = self._request(
                "get",
                self._api_url(f"issue/{key}") + "?fields=labels,reporter",
                headers=self._headers(),
            )
            if resp.status_code == 200:
                fields = resp.json().get("fields", {})
                if label in fields.get("labels", []):
                    reporter = fields.get("reporter") or {}
                    author_email = reporter.get("emailAddress", "")
                    author_name = reporter.get("displayName", "Unknown")

        if author_email is None:
            return {"found": False}
        return {"found": True, "email": author_email, "displayName": author_name}

    def get_description_editors(self, key: str) -> list[str]:
        """Return email addresses of all users who edited the issue description.

        Walks the full changelog looking for description field changes.
        Returns a list of unique email addresses (may be empty if the
        description was never edited after creation).
        """
        editors: list[str] = []
        seen: set[str] = set()
        start_at = 0

        while True:
            resp = self._request(
                "get",
                self._api_url(f"issue/{key}/changelog"),
                headers=self._headers(),
                params={"startAt": start_at, "maxResults": 100},
            )
            self._check(resp)

            data = resp.json()
            for entry in data.get("values", []):
                if not any(item.get("field") == "description" for item in entry.get("items", [])):
                    continue
                author = entry.get("author", {})
                email = author.get("emailAddress", "")
                if not email:
                    account_id = author.get("accountId", "unknown")
                    email = f"missing-email:{account_id}"
                if email not in seen:
                    editors.append(email)
                    seen.add(email)

            total = data.get("total", 0)
            values = data.get("values", [])
            start_at += len(values)
            if start_at >= total or not values:
                break

        return editors

    def get_custom_field(self, key: str, *field_names: str) -> dict[str, object]:
        """Read custom fields by name. Returns ``{name: value}``."""
        field_ids = {name: self._resolve_field_id(name) for name in field_names}
        ids_csv = ",".join(field_ids.values())

        resp = self._request(
            "get",
            self._api_url(f"issue/{key}") + f"?fields={ids_csv}",
            headers=self._headers(),
        )
        self._check(resp)

        fields = resp.json().get("fields", {})
        result: dict[str, object] = {}
        for name, fid in field_ids.items():
            value = fields.get(fid)
            if isinstance(value, dict) and "value" in value:
                value = value["value"]
            result[name] = value
        return result

    def search_parent_epics(self, jql: str) -> list[str]:
        """Find parent Epic keys for issues matching a child JQL query."""
        parent_keys: set[str] = set()
        next_page_token: str | None = None
        search_url = self._api_url("search/jql")

        while True:
            payload: dict = {"jql": jql, "fields": ["parent"], "maxResults": 50}
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            resp = self._request(
                "post",
                search_url,
                headers=self._headers(),
                json=payload,
            )
            self._check(resp)

            data = resp.json()
            for issue in data.get("issues", []):
                parent = issue.get("fields", {}).get("parent")
                if parent and parent.get("key"):
                    parent_keys.add(parent["key"])

            if data.get("isLast", True) or not data.get("issues"):
                break
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return sorted(parent_keys)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def add_comment(
        self,
        key: str,
        body: str,
        *,
        visibility_group: str | None = None,
    ) -> bool:
        """Post a comment, optionally restricted to a visibility group.

        The body is plain text (with optional markdown markup, converted
        to ADF for the REST API).  Uses acli when no visibility
        restriction is needed.  Returns True on success, False on failure.
        """
        if self._acli_available and not visibility_group:
            try:
                acli_mod.run_acli(
                    "jira",
                    "workitem",
                    "comment",
                    "create",
                    "--key",
                    key,
                    "--body",
                    body,
                )
                log.info("Commented on %s (via acli)", key)
                return True
            except acli_mod.AcliError as exc:
                log.warning("acli comment failed, falling back to REST: %s", exc)

        payload: dict = {"body": text_to_adf(body)}
        if visibility_group:
            payload["visibility"] = {"type": "group", "value": visibility_group}

        resp = self._request(
            "post",
            self._api_url(f"issue/{key}/comment"),
            headers=self._headers(),
            json=payload,
        )
        if resp.status_code == 201:
            log.info("Commented on %s", key)
            return True
        log.warning("Failed to comment on %s: HTTP %d", key, resp.status_code)
        return False

    def edit_labels(
        self,
        key: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        """Add and/or remove labels on an issue (atomic update).

        Delegates to acli for add-only operations. Falls back to
        REST API for remove or mixed add+remove (acli ``edit --labels``
        replaces; the REST API supports atomic add/remove).
        """
        if not add and not remove:
            return

        if self._acli_available and add and not remove:
            try:
                acli_mod.run_acli(
                    "jira",
                    "workitem",
                    "edit",
                    "--key",
                    key,
                    "--labels",
                    ",".join(add),
                )
                log.debug("Labels updated on %s (via acli)", key)
                return
            except acli_mod.AcliError as exc:
                log.warning("acli edit_labels failed, falling back to REST: %s", exc)

        update: dict = {}
        if add:
            update.setdefault("labels", []).extend({"add": lbl} for lbl in add)
        if remove:
            update.setdefault("labels", []).extend({"remove": lbl} for lbl in remove)

        resp = self._request(
            "put",
            self._api_url(f"issue/{key}"),
            headers=self._headers(),
            json={"update": update},
        )
        self._check(resp, expected=(200, 204))
        log.debug("Labels updated on %s", key)

    def transition(self, key: str, status: str) -> None:
        """Transition an issue to a new status by name."""
        if self._acli_available:
            try:
                acli_mod.run_acli(
                    "jira",
                    "workitem",
                    "transition",
                    "--key",
                    key,
                    "--status",
                    status,
                )
                log.info("Transitioned %s to %s (via acli)", key, status)
                return
            except acli_mod.AcliError as exc:
                log.warning("acli transition failed, falling back to REST: %s", exc)

        resp = self._request(
            "get",
            self._api_url(f"issue/{key}/transitions"),
            headers=self._headers(),
        )
        self._check(resp)

        transition_id = None
        for t in resp.json().get("transitions", []):
            if t.get("name", "").lower() == status.lower():
                transition_id = t["id"]
                break
            if t.get("to", {}).get("name", "").lower() == status.lower():
                transition_id = t["id"]
                break

        if transition_id is None:
            available = [t.get("name", "") for t in resp.json().get("transitions", [])]
            raise JiraError(f"No transition to '{status}' found for {key}. Available: {available}")

        resp = self._request(
            "post",
            self._api_url(f"issue/{key}/transitions"),
            headers=self._headers(),
            json={"transition": {"id": transition_id}},
        )
        self._check(resp, expected=(200, 204))
        log.info("Transitioned %s to %s", key, status)

    def assign(self, key: str, assignee: str) -> None:
        """Assign an issue to a user by account ID or email."""
        if self._acli_available:
            try:
                acli_mod.run_acli(
                    "jira",
                    "workitem",
                    "assign",
                    "--key",
                    key,
                    "--assignee",
                    assignee,
                )
                log.info("Assigned %s to %s (via acli)", key, assignee)
                return
            except acli_mod.AcliError as exc:
                log.warning("acli assign failed, falling back to REST: %s", exc)

        account_id = self._resolve_account_id(assignee)
        resp = self._request(
            "put",
            self._api_url(f"issue/{key}/assignee"),
            headers=self._headers(),
            json={"accountId": account_id},
        )
        self._check(resp, expected=(200, 204))
        log.info("Assigned %s to %s", key, assignee)

    def create_issue(
        self,
        project: str,
        issue_type: str,
        summary: str,
        *,
        description: str = "",
        parent_epic: str = "",
        **extra_fields: object,
    ) -> str:
        """Create a new issue. Returns the issue key.

        Uses acli for simple creates (no extra_fields). Falls back to
        REST API when Epic Link or extra fields are needed.
        """
        if self._acli_available and not extra_fields and not parent_epic:
            try:
                args = [
                    "jira",
                    "workitem",
                    "create",
                    "--project",
                    project,
                    "--type",
                    issue_type,
                    "--summary",
                    summary,
                ]
                if description:
                    args.extend(["--description", description])
                result = acli_mod.run_acli(*args, json_output=True)
                data = json.loads(result.stdout)
                key = data.get("key", "")
                log.info("Created issue %s (via acli)", key)
                return key
            except (acli_mod.AcliError, json.JSONDecodeError, KeyError) as exc:
                log.warning("acli create failed, falling back to REST: %s", exc)

        fields: dict = {
            "project": {"key": project},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = text_to_adf(description)
        if parent_epic:
            epic_field = self._resolve_field_id("Epic Link")
            fields[epic_field] = parent_epic
        fields.update(extra_fields)

        resp = self._request(
            "post",
            self._api_url("issue"),
            headers=self._headers(),
            json={"fields": fields},
        )
        self._check(resp, expected=201)
        key = resp.json().get("key", "")
        log.info("Created issue %s", key)
        return key

    def link_issues(self, source: str, target: str, link_type: str) -> None:
        """Link two issues with a named link type."""
        if self._acli_available:
            try:
                acli_mod.run_acli(
                    "jira",
                    "workitem",
                    "link",
                    "create",
                    "--out",
                    source,
                    "--in",
                    target,
                    "--type",
                    link_type,
                )
                log.info("Linked %s '%s' %s (via acli)", source, link_type, target)
                return
            except acli_mod.AcliError as exc:
                log.warning("acli link failed, falling back to REST: %s", exc)

        resp = self._request(
            "get",
            self._api_url("issueLinkType"),
            headers=self._headers(),
        )
        self._check(resp)

        resolved_name = None
        direction = "outward"
        link_lower = link_type.lower()
        for lt in resp.json().get("issueLinkTypes", []):
            if lt.get("name", "").lower() == link_lower:
                resolved_name = lt["name"]
                direction = "outward"
                break
            if lt.get("inward", "").lower() == link_lower:
                resolved_name = lt["name"]
                direction = "inward"
                break
            if lt.get("outward", "").lower() == link_lower:
                resolved_name = lt["name"]
                direction = "outward"
                break

        if not resolved_name:
            available = [lt.get("name", "") for lt in resp.json().get("issueLinkTypes", [])]
            raise JiraError(f"Link type '{link_type}' not found. Available: {', '.join(available)}")

        if direction == "inward":
            payload = {
                "type": {"name": resolved_name},
                "inwardIssue": {"key": source},
                "outwardIssue": {"key": target},
            }
        else:
            payload = {
                "type": {"name": resolved_name},
                "inwardIssue": {"key": target},
                "outwardIssue": {"key": source},
            }

        resp = self._request(
            "post",
            self._api_url("issueLink"),
            headers=self._headers(),
            json=payload,
        )
        self._check(resp, expected=201)
        log.info("Linked %s '%s' %s", source, link_type, target)

    def attach_file(self, key: str, filepath: str | Path) -> None:
        """Attach a file to an issue."""
        path = Path(filepath)
        if not path.is_file():
            raise JiraError(f"File '{filepath}' not found or not readable")

        with open(path, "rb") as f:
            resp = self._request(
                "post",
                self._api_url(f"issue/{key}/attachments"),
                headers={"X-Atlassian-Token": "no-check"},
                files={"file": (path.name, f)},
            )
        self._check(resp)
        log.info("Attached '%s' to %s", path.name, key)

    def _get_field_schema_type(self, field_name: str) -> str:
        """Return the schema type string for a field (e.g. 'string', 'option')."""
        self._load_field_metadata()
        assert self._field_schema_cache is not None
        return self._field_schema_cache.get(field_name, "")

    def set_custom_field(self, key: str, field_name: str, value: str) -> None:
        """Set a custom field by name.

        Consults the field schema to determine value format: text/string
        fields get the raw string, option/select fields get ``{"value": v}``.
        If the value is valid JSON, it is sent as-is.
        """
        field_id = self._resolve_field_id(field_name)

        parsed_value: str | dict | list | int | float | bool | None
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError:
            schema_type = self._get_field_schema_type(field_name)
            if schema_type in ("string", "any", ""):
                parsed_value = value
            else:
                parsed_value = {"value": value}

        resp = self._request(
            "put",
            self._api_url(f"issue/{key}"),
            headers=self._headers(),
            json={"fields": {field_id: parsed_value}},
        )
        self._check(resp, expected=(200, 204))
        log.info("Set '%s' on %s", field_name, key)
