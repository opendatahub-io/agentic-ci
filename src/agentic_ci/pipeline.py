"""GitLab child pipeline YAML generation.

Generates YAML for child pipelines that process one ticket (or work
item) per job.  The templates are parameterised so any project can
use the same slot-distribution and noop-pipeline patterns.
"""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from typing import Callable

_SAFE_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def distribute_slot(key: str, max_concurrency: int, prefix: str = "slot") -> str:
    """Deterministically assign a key to a resource-group slot.

    Distributes keys evenly across ``max_concurrency`` numbered slots
    using a SHA-256 hash.
    """
    if not isinstance(max_concurrency, int) or max_concurrency <= 0:
        raise ValueError("max_concurrency must be a positive integer")
    digest = int(hashlib.sha256(key.encode()).hexdigest(), 16)
    slot_num = (digest % max_concurrency) + 1
    return f"{prefix}-{slot_num}"


def noop_pipeline(message: str) -> str:
    """Generate a minimal pipeline that prints a message and exits."""
    safe_message = json.dumps(message)
    return textwrap.dedent(f"""\
        no-tickets:
          image: alpine:latest
          script:
            - echo {safe_message}
    """)


def generate_child_pipeline(
    items: list[dict],
    *,
    job_name_fn: Callable[..., str] | None = None,
    job_body_fn: Callable[..., str] | None = None,
    default_job_yaml: str = "",
    max_concurrency: int = 3,
    slot_prefix: str = "slot",
    noop_message: str = "No items to process",
) -> str:
    """Generate a GitLab child pipeline with one job per item.

    Args:
        items: List of dicts, each must have a ``"key"`` field.
        job_name_fn: ``(item) -> str`` returning the job name.
            Defaults to ``item.get("key", "unknown")``.
        job_body_fn: ``(item, slot) -> str`` returning the YAML body
            for one job (indented, without the job name line).
        default_job_yaml: YAML for the ``.default-job`` template
            (prepended to the output).
        max_concurrency: Number of resource-group slots.
        slot_prefix: Prefix for resource-group slot names.
        noop_message: Message for the noop pipeline when items is empty.

    Returns:
        Complete YAML string.
    """
    if not items:
        return noop_pipeline(noop_message)

    if job_name_fn is None:

        def job_name_fn(item: dict) -> str:
            return item.get("key", "unknown")

    if job_body_fn is None:
        raise ValueError("job_body_fn is required")

    lines: list[str] = []
    if default_job_yaml:
        lines.append(default_job_yaml)

    for item in items:
        key = item.get("key", "unknown")
        slot = distribute_slot(key, max_concurrency, slot_prefix)
        name = job_name_fn(item)
        if not _SAFE_JOB_NAME_RE.match(name):
            raise ValueError(
                f"Job name {name!r} contains invalid characters. "
                "Only alphanumerics, dots, hyphens, and underscores are allowed."
            )
        body = job_body_fn(item, slot)
        lines.append(f"{name}:\n{body}")

    return "\n".join(lines)
