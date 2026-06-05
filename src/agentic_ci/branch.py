"""Branch resolution for version-aware CI workflows.

This module provides functionality to resolve target branches from Jira tickets
using fixVersion fields and component configuration overrides.
"""

from __future__ import annotations

import logging
from typing import Any

from agentic_ci.git import validate_branch_exists

log = logging.getLogger(__name__)


class BranchResolutionError(Exception):
    """Raised when branch resolution or validation fails critically."""


def resolve_branch_from_jira(
    ticket: dict[str, Any],
    component_config: dict[str, Any] | None = None,
    repo_url: str | None = None,
) -> str | None:
    """Resolve target branch for a Jira ticket using fixVersion and component config.

    Checks the ticket's fixVersions field against the component's
    version_branches map to determine the correct target branch for MRs/PRs.

    Resolution order:
    1. fixVersion[0] mapped through component_config.version_branches
    2. fixVersion[0] used directly as branch name
    3. component_config.branch static fallback
    4. None (caller uses repo default branch)

    Each candidate is validated against the remote (if repo_url is provided)
    before being returned. Returns None on validation failure so the caller
    can fall back to the repository default branch.
    """
    log.info(
        "Branch resolution started: fixVersions=%s, component=%s, repo=%s",
        ticket.get("fixVersions", []),
        component_config.get("name") if component_config else None,
        repo_url,
    )

    # Step 1: Extract fixVersion[0]
    fix_versions = ticket.get("fixVersions", [])
    fix_version_name = None

    if fix_versions:
        first_version = fix_versions[0]
        if isinstance(first_version, dict) and "name" in first_version:
            fix_version_name = first_version["name"]
            if fix_version_name:  # Ensure it's not empty
                log.info("Found fixVersion: %s", fix_version_name)
            else:
                log.debug("fixVersion name is empty, will try fallback")
                fix_version_name = None
        else:
            log.debug("fixVersion object missing 'name' field, will try fallback")
    else:
        log.debug("No fixVersions found in ticket, will try fallback")

    resolved_branch = None

    if fix_version_name:
        # Step 2: Check version_branches override
        if component_config and "version_branches" in component_config:
            version_branches = component_config["version_branches"]
            if fix_version_name in version_branches:
                resolved_branch = version_branches[fix_version_name]
                log.info(
                    "Using version_branches override: %s -> %s", fix_version_name, resolved_branch
                )
            else:
                # Step 3: Use fixVersion directly
                resolved_branch = fix_version_name
                log.info("Using fixVersion directly as branch: %s", resolved_branch)
        else:
            # Step 3: Use fixVersion directly (no component config or version_branches)
            resolved_branch = fix_version_name
            log.info("Using fixVersion directly as branch: %s", resolved_branch)

    # Step 4: Fall back to component.branch
    if not resolved_branch and component_config and "branch" in component_config:
        resolved_branch = component_config["branch"]
        log.info("Falling back to component branch: %s", resolved_branch)

    if not resolved_branch:
        log.info("No branch could be resolved from ticket or component config")
        return None

    # Step 5: Validate branch exists (if repo_url provided)
    if repo_url:
        if not validate_branch_exists(repo_url, resolved_branch):
            log.warning(
                "Resolved branch %s does not exist on remote %s, returning None",
                resolved_branch,
                repo_url,
            )
            return None
    else:
        log.debug("No repo URL provided, skipping branch validation")

    log.info("Successfully resolved branch: %s", resolved_branch)
    return resolved_branch
