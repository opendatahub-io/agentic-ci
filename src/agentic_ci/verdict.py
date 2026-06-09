"""Verdict JSON schema validation and loading.

Provides a generic framework for loading and validating structured
verdict files produced by AI agent runs.  Callers define their own
schemas (required fields, allowed verdict values) and this module
handles file I/O, JSON parsing, and schema validation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_LIST_FIELDS = ("files_changed", "risks", "blockers", "observations")


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

    for list_field in _LIST_FIELDS:
        val = data.get(list_field)
        if val is not None and not isinstance(val, list):
            if isinstance(val, str):
                log.warning(
                    "%s verdict field %s is a string, coercing to array",
                    name,
                    list_field,
                )
                data[list_field] = [val]
            else:
                raise VerdictError(
                    f"{name} verdict field {list_field} must be an array or null, got {type(val)}"
                )

    return data
