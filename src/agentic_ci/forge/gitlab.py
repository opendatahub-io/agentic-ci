"""GitLab forge implementation.

Provides ``GitLabForge`` for interacting with the GitLab REST API
(merge requests, pipelines, discussions).
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime

from agentic_ci.forge import (
    DEFAULT_SKIP_PATTERNS,
    Forge,
    ForgeError,
    parse_gitlab_mr_url,
    repo_path_from_url,
)
from agentic_ci.forge.session import build_session, extract_api_error

log = logging.getLogger(__name__)


class GitLabForge(Forge):
    """GitLab REST API implementation of the ``Forge`` interface."""

    def __init__(self) -> None:
        self._session = build_session()

    def project_id(self, project_path: str) -> int:
        """Look up the numeric GitLab project ID from a project path.

        Raises ``ForgeError`` if the project cannot be found.
        """
        encoded = urllib.parse.quote(project_path, safe="")
        resp = self._session.get(f"https://gitlab.com/api/v4/projects/{encoded}")
        if resp.status_code != 200:
            raise ForgeError(
                f"HTTP {resp.status_code} looking up project {project_path}: {resp.text}"
            )
        return resp.json()["id"]

    def create_merge_request(
        self,
        repo_url: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> tuple[str | None, str | None]:
        project_path = repo_path_from_url(repo_url)
        pid = self.project_id(project_path)

        existing = self._session.get(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests",
            params={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "state": "opened",
            },
        )
        if existing.status_code == 200:
            mrs = existing.json()
            if mrs:
                existing_url = mrs[0].get("web_url")
                log.info("Found existing open MR: %s", existing_url)
                return existing_url, None

        payload: dict[str, str | bool] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": True,
        }
        resp = self._session.post(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests",
            json=payload,
        )
        if resp.status_code not in (200, 201):
            error = extract_api_error(resp)
            log.error("HTTP %d creating MR: %s", resp.status_code, error)
            return None, error
        return resp.json().get("web_url"), None

    def mr_status(self, mr_url: str) -> dict:
        project_path, mr_iid = parse_gitlab_mr_url(mr_url)
        pid = self.project_id(project_path)
        resp = self._session.get(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests/{mr_iid}",
        )
        if resp.status_code != 200:
            raise ForgeError(f"HTTP {resp.status_code}: {resp.text}")
        mr = resp.json()
        state = mr.get("state", "unknown")
        if state == "opened":
            state = "open"
        pipeline_status = "unknown"
        pipelines_resp = self._session.get(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests/{mr_iid}/pipelines",
            params={"per_page": 1},
        )
        if pipelines_resp.status_code == 200:
            pipelines = pipelines_resp.json()
            if pipelines:
                pipeline_status = pipelines[0].get("status", "unknown")
        return {
            "state": state,
            "source_branch": mr.get("source_branch", ""),
            "pipeline_status": pipeline_status,
        }

    def update_description(
        self,
        mr_url: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        payload: dict[str, str] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if not payload:
            return
        project_path, mr_iid = parse_gitlab_mr_url(mr_url)
        pid = self.project_id(project_path)
        resp = self._session.put(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests/{mr_iid}",
            json=payload,
        )
        if resp.status_code != 200:
            error = extract_api_error(resp)
            raise ForgeError(f"HTTP {resp.status_code} updating MR: {error}")

    def review_comments(self, mr_url: str) -> list[dict]:
        project_path, mr_iid = parse_gitlab_mr_url(mr_url)
        pid = self.project_id(project_path)
        all_discussions = self._paginate(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests/{mr_iid}/discussions",
        )
        threads = []
        for disc in all_discussions:
            if disc.get("individual_note", True):
                continue
            notes = disc.get("notes", [])
            if not notes:
                continue
            first_note = notes[0]
            if first_note.get("resolved", False):
                continue
            position = first_note.get("position")
            if not position:
                continue
            file_path = position.get("new_path", position.get("old_path", ""))
            new_line = position.get("new_line")
            old_line = position.get("old_line")
            line = new_line or old_line or 0
            body_parts = []
            for note in notes:
                author = note.get("author", {}).get("name", "Unknown")
                body_parts.append(f"{author}: {note.get('body', '')}")
            threads.append(
                {
                    "thread_id": disc["id"],
                    "file": file_path,
                    "line": line,
                    "body": "\n".join(body_parts),
                    "author": first_note.get("author", {}).get("name", "Unknown"),
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
        project_path, mr_iid = parse_gitlab_mr_url(mr_url)
        pid = self.project_id(project_path)
        all_discussions = self._paginate(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests/{mr_iid}/discussions",
        )
        comments = []
        for disc in all_discussions:
            notes = disc.get("notes", [])
            if not notes:
                continue
            note = notes[0]
            if note.get("position") or note.get("system", False):
                continue
            body = note.get("body", "")
            if any(pat in body for pat in skip_patterns):
                continue
            created = note.get("created_at", "")
            if since and created:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if created_dt < since_dt:
                    continue
            comments.append(
                {
                    "author": note.get("author", {}).get("name", "Unknown"),
                    "body": body,
                    "created_at": created,
                }
            )
        return comments

    def reply(self, mr_url: str, thread_id: str, message: str) -> None:
        project_path, mr_iid = parse_gitlab_mr_url(mr_url)
        pid = self.project_id(project_path)
        resp = self._session.post(
            f"https://gitlab.com/api/v4/projects/{pid}"
            f"/merge_requests/{mr_iid}/discussions/{thread_id}/notes",
            json={"body": message},
        )
        if resp.status_code not in (200, 201):
            raise ForgeError(f"HTTP {resp.status_code}: {resp.text}")

    def resolve(self, mr_url: str, thread_id: str) -> None:
        project_path, mr_iid = parse_gitlab_mr_url(mr_url)
        pid = self.project_id(project_path)
        resp = self._session.put(
            f"https://gitlab.com/api/v4/projects/{pid}"
            f"/merge_requests/{mr_iid}/discussions/{thread_id}",
            json={"resolved": True},
        )
        if resp.status_code != 200:
            raise ForgeError(f"HTTP {resp.status_code}: {resp.text}")

    def pipeline_failures(self, mr_url: str) -> dict:
        project_path, mr_iid = parse_gitlab_mr_url(mr_url)
        pid = self.project_id(project_path)
        pipelines_resp = self._session.get(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests/{mr_iid}/pipelines",
            params={"per_page": 1},
        )
        if pipelines_resp.status_code != 200:
            raise ForgeError(f"HTTP {pipelines_resp.status_code}: {pipelines_resp.text}")
        pipelines = pipelines_resp.json()
        if not pipelines:
            return {"pipeline_status": "none", "failed_jobs": []}
        pipeline = pipelines[0]
        pipeline_id = pipeline["id"]
        pipeline_status = pipeline.get("status", "unknown")
        if pipeline_status == "success":
            return {"pipeline_status": "success", "failed_jobs": []}
        jobs_resp = self._session.get(
            f"https://gitlab.com/api/v4/projects/{pid}/pipelines/{pipeline_id}/jobs",
            params={"per_page": "100", "scope[]": "failed"},
        )
        if jobs_resp.status_code != 200:
            raise ForgeError(f"HTTP {jobs_resp.status_code}: {jobs_resp.text}")
        failed_jobs = []
        for job in jobs_resp.json():
            job_name = job.get("name", "unknown")
            job_id = job["id"]
            trace_resp = self._session.get(
                f"https://gitlab.com/api/v4/projects/{pid}/jobs/{job_id}/trace",
            )
            log_text = ""
            if trace_resp.status_code == 200:
                lines = trace_resp.text.splitlines()
                log_text = "\n".join(lines[-200:])
            failed_jobs.append({"name": job_name, "id": job_id, "log": log_text})
        return {"pipeline_status": pipeline_status, "failed_jobs": failed_jobs}

    def mr_diff_position(self, mr_url: str) -> dict:
        """Get the first changed line position and diff refs from a GitLab MR.

        This is a GitLab-specific operation not available on other forges.
        Returns ``{"file", "line", "base_sha", "head_sha", "start_sha"}``.
        """
        project_path, mr_iid = parse_gitlab_mr_url(mr_url)
        pid = self.project_id(project_path)
        resp = self._session.get(
            f"https://gitlab.com/api/v4/projects/{pid}/merge_requests/{mr_iid}/changes",
        )
        if resp.status_code != 200:
            raise ForgeError(f"HTTP {resp.status_code}: {resp.text}")
        data = resp.json()
        diff_refs = data.get("diff_refs", {})
        result = {
            "file": "",
            "line": 0,
            "base_sha": diff_refs.get("base_sha", ""),
            "head_sha": diff_refs.get("head_sha", ""),
            "start_sha": diff_refs.get("start_sha", ""),
        }
        for change in data.get("changes", []):
            diff_text = change.get("diff", "")
            new_path = change.get("new_path", "")
            if not diff_text or not new_path:
                continue
            line = _find_first_added_line(diff_text)
            if line is not None:
                result["file"] = new_path
                result["line"] = line
                break
        return result

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages of a paginated GitLab API endpoint."""
        all_items: list[dict] = []
        page = 1
        while True:
            page_params = {"per_page": 100, "page": page}
            if params:
                page_params.update(params)
            resp = self._session.get(url, params=page_params)
            if resp.status_code != 200:
                raise ForgeError(f"HTTP {resp.status_code}: {resp.text}")
            batch = resp.json()
            if not batch:
                break
            all_items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return all_items


def _find_first_added_line(diff_text: str) -> int | None:
    """Parse unified diff to find the line number of the first added line."""
    for hunk in re.finditer(
        r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@",
        diff_text,
    ):
        line_num = int(hunk.group(1))
        nl_pos = diff_text.find("\n", hunk.end())
        if nl_pos == -1:
            continue
        hunk_start = nl_pos + 1
        next_hunk = re.search(r"\n@@", diff_text[hunk_start:])
        hunk_end = hunk_start + next_hunk.start() if next_hunk else len(diff_text)
        for hl in diff_text[hunk_start:hunk_end].split("\n"):
            if hl.startswith("\\"):
                continue
            if hl.startswith("-"):
                continue
            if hl.startswith("+"):
                return line_num
            if hl:
                line_num += 1
    return None
