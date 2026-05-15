"""Jira client (acli-first with REST fallback) and ADF conversion utilities."""

from agentic_ci.jira.acli import AcliError, ensure_acli, is_available, setup_auth
from agentic_ci.jira.adf import adf_to_text, text_to_adf
from agentic_ci.jira.client import JiraClient

__all__ = [
    "AcliError",
    "JiraClient",
    "adf_to_text",
    "ensure_acli",
    "is_available",
    "setup_auth",
    "text_to_adf",
]
