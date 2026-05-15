"""Verdict JSON schema validation and loading.

Provides a generic framework for loading and validating structured
verdict files produced by AI agent runs.  Callers define their own
schemas (required fields, allowed verdict values) and this module
handles file I/O, JSON parsing, and schema validation.
"""

from __future__ import annotations

import json
from pathlib import Path


class VerdictError(Exception):
    """Raised when a verdict file is missing, malformed, or invalid."""


def load_verdict(
    path: str | Path,
    *,
    required_fields: set[str],
    allowed_verdicts: frozenset[str],
    name: str = "verdict",
) -> dict:
    """Load and validate a verdict JSON file.

    Args:
        path: Path to the verdict JSON file.
        required_fields: Set of field names that must be present.
        allowed_verdicts: Set of allowed values for the ``verdict`` field.
        name: Human-readable name for error messages.

    Returns:
        The parsed verdict dict.

    Raises:
        VerdictError: If the file is missing, malformed, or schema-invalid.
    """
    p = Path(path)
    if not p.exists():
        raise VerdictError(f"{name} verdict file not found: {p}")

    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise VerdictError(f"Cannot read {name} verdict file: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise VerdictError(f"Malformed JSON in {name} verdict: {e}") from e

    if not isinstance(data, dict):
        raise VerdictError(f"{name} verdict must be a JSON object, got {type(data)}")

    for field in required_fields:
        if field not in data:
            raise VerdictError(f"{name} verdict missing required field: {field}")

    verdict_val = data.get("verdict", "")
    if verdict_val not in allowed_verdicts:
        raise VerdictError(
            f"Unknown {name} verdict value: {verdict_val!r}. Allowed: {sorted(allowed_verdicts)}"
        )

    for bool_field in ("lint_passed", "build_passed", "tests_passed"):
        val = data.get(bool_field)
        if val is not None and not isinstance(val, bool):
            raise VerdictError(
                f"{name} verdict field {bool_field} must be bool or null, got {type(val)}"
            )

    for list_field in ("files_changed", "risks", "blockers", "observations"):
        val = data.get(list_field)
        if val is not None and not isinstance(val, list):
            raise VerdictError(
                f"{name} verdict field {list_field} must be an array or null, got {type(val)}"
            )

    return data


# ---------------------------------------------------------------------------
# Convenience loaders with pre-defined schemas
# ---------------------------------------------------------------------------

AUTOFIX_VERDICTS = frozenset(
    {
        "committed",
        "already_fixed",
        "not_a_bug",
        "insufficient_info",
        "blocked",
        "research",
        "no_changes",
    }
)

TRIAGE_VERDICTS = frozenset(
    {
        "ready",
        "needs_info",
        "not_fixable",
    }
)

REQUIRED_AUTOFIX_FIELDS = {"verdict", "summary"}
REQUIRED_TRIAGE_FIELDS = {"verdict"}


def load_autofix_verdict(work_dir: str | Path) -> dict:
    """Load and validate ``autofix-output/.autofix-verdict.json``."""
    path = Path(work_dir) / "autofix-output" / ".autofix-verdict.json"
    return load_verdict(
        path,
        required_fields=REQUIRED_AUTOFIX_FIELDS,
        allowed_verdicts=AUTOFIX_VERDICTS,
        name="autofix",
    )


def load_triage_verdict(work_dir: str | Path) -> dict:
    """Load and validate ``.triage-verdict.json``."""
    path = Path(work_dir) / ".triage-verdict.json"
    return load_verdict(
        path,
        required_fields=REQUIRED_TRIAGE_FIELDS,
        allowed_verdicts=TRIAGE_VERDICTS,
        name="triage",
    )
