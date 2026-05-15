"""GitLab child pipeline YAML generation.

Generates YAML for child pipelines that process one ticket (or work
item) per job.  The templates are parameterised so any project can
use the same slot-distribution and noop-pipeline patterns.
"""

from __future__ import annotations

import hashlib
import textwrap


def distribute_slot(key: str, max_concurrency: int, prefix: str = "slot") -> str:
    """Deterministically assign a key to a resource-group slot.

    Distributes keys evenly across ``max_concurrency`` numbered slots
    using a SHA-256 hash.
    """
    digest = int(hashlib.sha256(key.encode()).hexdigest(), 16)
    slot_num = (digest % max_concurrency) + 1
    return f"{prefix}-{slot_num}"


def noop_pipeline(message: str) -> str:
    """Generate a minimal pipeline that prints a message and exits."""
    return textwrap.dedent(f"""\
        no-tickets:
          image: alpine:latest
          script:
            - echo "{message}"
    """)


def generate_child_pipeline(
    items: list[dict],
    *,
    job_name_fn: object = None,
    job_body_fn: object = None,
    default_job_yaml: str = "",
    max_concurrency: int = 3,
    slot_prefix: str = "slot",
    noop_message: str = "No items to process",
) -> str:
    """Generate a GitLab child pipeline with one job per item.

    Args:
        items: List of dicts, each must have a ``"key"`` field.
        job_name_fn: ``(item) -> str`` returning the job name.
            Defaults to ``item["key"]``.
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

        def job_name_fn(item):
            return item["key"]

    if job_body_fn is None:
        raise ValueError("job_body_fn is required")

    lines = []
    if default_job_yaml:
        lines.append(default_job_yaml)

    for item in items:
        key = item.get("key", "unknown")
        slot = distribute_slot(key, max_concurrency, slot_prefix)
        name = job_name_fn(item)
        body = job_body_fn(item, slot)
        lines.append(f"{name}:\n{body}")

    return "\n".join(lines)
