"""Generic skill runner framework.

Provides ``SkillConfig`` and ``run_skill()`` — a reusable pipeline for
running AI agent skills against tickets/issues in CI. All domain-specific
behavior is injected via callable hooks on ``SkillConfig``, making this
framework agnostic to the issue tracker, git forge, and skill content.

Usage::

    from agentic_ci.skill import SkillConfig, run_skill

    config = SkillConfig(
        skill_name="my-resolve",
        prompt_builder=my_prompt_fn,
        verdict_loader=my_verdict_fn,
        label_applier=my_label_fn,
    )
    rc = run_skill(config, ticket_key="PROJ-123", work_dir=Path("/tmp/work"), ...)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

TRANSIENT_EXIT_CODES = frozenset({124, 137, 143})


def _noop(**_kw):
    pass


def _noop_verdict(_work_dir):
    return {}


@dataclass
class SkillConfig:
    """Configuration for a skill run. All domain-specific behavior via hooks."""

    skill_name: str
    skill_source: str = ""
    skill_ref: str = "main"

    prompt_builder: Callable[..., str] = lambda **kw: ""
    context_writer: Callable[..., None] = _noop
    verdict_loader: Callable[..., dict] = _noop_verdict
    verdict_path_fn: Callable[[Path], Path] = lambda wd: wd / "verdict.json"
    comment_formatter: Callable[[dict], str] = lambda v: str(v)
    label_applier: Callable[..., None] = _noop
    cost_formatter: Callable[[dict | None], str | None] = lambda d: None
    extension_config_writer: Callable[..., None] = _noop

    pre_gates: list[Callable[..., str | None]] = field(default_factory=list)
    post_gates: list[Callable[..., tuple[dict | None, list[str]]]] = field(default_factory=list)

    extra_skills: list[str] = field(default_factory=list)

    max_retries: int = 1
    retryable_modes: frozenset[str] = frozenset({"resolve"})

    container_image: str | None = None
    container_runner: Callable[..., int] | None = None


def _load_otel_cost(work_dir: Path) -> dict | None:
    """Load OTEL cost data from the run directory, if available."""
    otel_log = work_dir / "_run" / "claude-otel.jsonl"
    try:
        otel_log.resolve().relative_to(work_dir.resolve())
    except ValueError:
        log.warning("OTEL log path escapes work_dir, skipping: %s", otel_log)
        return None
    if not otel_log.exists():
        return None
    try:
        from agentic_ci.otel import parse_metrics

        records = []
        with open(otel_log, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        if not records:
            return None
        token_totals, cost_totals, api_requests, active_time = parse_metrics(records)
        return {
            "token_totals": dict(token_totals),
            "cost_totals": dict(cost_totals),
            "api_requests": api_requests,
            "active_time": dict(active_time),
        }
    except Exception as exc:
        log.warning("Failed to parse OTEL data: %s", exc)
        return None


def _default_run_container(work_dir, prompt, output_file, *, image=None):
    """Default container runner using PodmanBackend."""
    from agentic_ci.backends.podman import PodmanBackend

    model = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")
    backend = PodmanBackend(workdir=str(work_dir), image=image)
    try:
        backend.setup()
        return backend.run(prompt, model=model)
    finally:
        backend.stop()


def run_skill(
    config: SkillConfig,
    ticket_key: str,
    work_dir: Path,
    config_dir: Path,
    *,
    mode: str = "resolve",
    ticket: dict | None = None,
    dry_run: bool = False,
    dry_run_verdict_path: Path | None = None,
    **extra_kwargs,
) -> int:
    """Run a skill pipeline for a single ticket. Returns exit code.

    Flow:
    1. Run pre-gates (skip container if any gate returns a non-None message)
    2. Write context via context_writer hook
    3. Write extension config via extension_config_writer hook
    4. Build prompt via prompt_builder hook
    5. Launch container (or dry-run)
    6. Read cost data (OTEL)
    7. Run post-gates
    8. Load verdict via verdict_loader hook
    9. Format comment and apply labels via label_applier hook
    """
    log.info("[%s] Starting %s in %s mode", ticket_key, config.skill_name, mode)

    for gate in config.pre_gates:
        result = gate(
            ticket_key=ticket_key,
            ticket=ticket,
            mode=mode,
            work_dir=work_dir,
            **extra_kwargs,
        )
        if result is not None:
            log.info("[%s] Pre-gate blocked: %s", ticket_key, result)
            return 0

    config.context_writer(
        ticket_key=ticket_key,
        ticket=ticket,
        mode=mode,
        work_dir=work_dir,
        **extra_kwargs,
    )

    config.extension_config_writer(
        ticket_key=ticket_key,
        ticket=ticket,
        config=config,
        work_dir=work_dir,
        **extra_kwargs,
    )

    prompt = config.prompt_builder(
        ticket_key=ticket_key,
        mode=mode,
        skill_name=config.skill_name,
        **extra_kwargs,
    )
    output_file = work_dir / "claude-output.txt"

    runner = config.container_runner or _default_run_container

    if dry_run:
        if dry_run_verdict_path:
            verdict_dest = config.verdict_path_fn(work_dir)
            verdict_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dry_run_verdict_path, verdict_dest)
        rc = 0
    else:
        rc = runner(work_dir, prompt, output_file, image=config.container_image)

        attempt = 0
        while (
            rc != 0
            and mode in config.retryable_modes
            and rc in TRANSIENT_EXIT_CODES
            and attempt < config.max_retries
        ):
            attempt += 1
            log.warning(
                "[%s] Transient failure (exit %d), retry %d/%d",
                ticket_key,
                rc,
                attempt,
                config.max_retries,
            )
            rc = runner(work_dir, prompt, output_file, image=config.container_image)

    if rc != 0:
        log.error("[%s] Container exited with code %d", ticket_key, rc)
        config.label_applier(
            ticket_key=ticket_key,
            verdict=None,
            rc=rc,
            mode=mode,
            work_dir=work_dir,
            **extra_kwargs,
        )
        return rc

    cost_data = _load_otel_cost(work_dir)

    gate_errors: list[str] = []
    verdict = None
    for gate in config.post_gates:
        v, errors = gate(work_dir=work_dir, ticket_key=ticket_key, **extra_kwargs)
        if v is not None:
            verdict = v
        gate_errors.extend(errors)

    if gate_errors:
        log.error("[%s] Post-gate failures: %s", ticket_key, gate_errors)
        config.label_applier(
            ticket_key=ticket_key,
            verdict=None,
            gate_errors=gate_errors,
            mode=mode,
            work_dir=work_dir,
            **extra_kwargs,
        )
        return 1

    if verdict is None:
        verdict_error: Exception | None = None
        try:
            verdict = config.verdict_loader(work_dir)
        except Exception as exc:
            verdict_error = exc
            if not dry_run and mode in config.retryable_modes and config.max_retries > 0:
                log.warning("[%s] Verdict missing (%s), retrying once", ticket_key, exc)
                rc = runner(
                    work_dir,
                    prompt,
                    output_file,
                    image=config.container_image,
                )
                if rc == 0:
                    try:
                        verdict = config.verdict_loader(work_dir)
                        verdict_error = None
                    except Exception as retry_exc:
                        log.error(
                            "[%s] Verdict still missing after retry: %s", ticket_key, retry_exc
                        )
                        verdict_error = retry_exc

            if verdict is None:
                log.error("[%s] Failed to load verdict: %s", ticket_key, verdict_error)
                config.label_applier(
                    ticket_key=ticket_key,
                    verdict=None,
                    gate_errors=[str(verdict_error)],
                    mode=mode,
                    work_dir=work_dir,
                    **extra_kwargs,
                )
                return 1

    cost_summary = config.cost_formatter(cost_data)
    if cost_summary:
        verdict["_cost_summary"] = cost_summary

    config.label_applier(
        ticket_key=ticket_key,
        verdict=verdict,
        mode=mode,
        work_dir=work_dir,
        **extra_kwargs,
    )

    log.info(
        "[%s] %s complete: verdict=%s",
        ticket_key,
        config.skill_name,
        verdict.get("verdict", "unknown"),
    )
    return 0
