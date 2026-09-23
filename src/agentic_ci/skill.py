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

``run_routed_skill()`` wraps the same pipeline with difficulty-based model
routing: a classifier run on the default model rates the task, and the
skill then runs on the matching ``ModelTier`` (see ``agentic_ci.routing``).
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypedDict

from agentic_ci.backends import create_backend
from agentic_ci.harness import create_harness
from agentic_ci.otel import (
    generate_trace_context,
    inject_root_spans,
    parse_metrics,
    root_span_attributes,
    start_collector,
    stop_collector,
)
from agentic_ci.routing import (
    DEFAULT_CLASSIFIER_MAX_TURNS,
    ModelTier,
    RouteDecision,
    classify,
    forced_route,
    resolve_model_tiers,
    route_path,
)
from agentic_ci.telemetry import emit_event

log = logging.getLogger(__name__)

TRANSIENT_EXIT_CODES = frozenset({124, 137, 143})


def _noop(**_kw):
    pass


def _noop_verdict(_work_dir):
    return {}


class _SkillHookRequired(TypedDict):
    name: str


class SkillHook(_SkillHookRequired, total=False):
    """Structured definition of an extra skill to run at a pipeline hook point."""

    args: str
    hooks: list[str]


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
    label_applier: Callable[..., int | None] = _noop
    cost_formatter: Callable[[dict | None], str | None] = lambda d: None
    extension_config_writer: Callable[..., None] = _noop

    pre_gates: list[Callable[..., str | None]] = field(default_factory=list)
    post_gates: list[Callable[..., tuple[dict | None, list[str]]]] = field(default_factory=list)

    extra_skills: list[SkillHook] = field(default_factory=list)
    context_dir: str = ".context"
    artifacts: list[str] = field(default_factory=list)

    max_retries: int = 1
    retryable_modes: frozenset[str] = frozenset({"resolve"})

    backend_name: str = "podman"
    harness_name: str = "claude-code"
    container_image: str | None = None
    container_env: dict[str, str] = field(default_factory=dict)
    container_runner: Callable[..., int] | None = None

    model_tiers: dict[str, ModelTier] = field(default_factory=dict)
    """Per-tier overrides of the harness default routing tiers (``run_routed_skill`` only)."""


@dataclass(frozen=True)
class RoutedSkillResult:
    """Result of :func:`run_routed_skill`.

    ``route`` is ``None`` when no agent ran (dry run or a pre-gate blocked
    the run), so no routing decision was made.
    """

    rc: int
    route: RouteDecision | None


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


class _AgentSession:
    """One backend lifecycle: setup once, run the agent N times, stop once.

    Owns the OTEL collector and the synthetic root span so that every
    ``run()`` inside the session (for example a classifier run followed by
    the real skill run) lands in a single trace.
    """

    def __init__(
        self,
        work_dir,
        *,
        image=None,
        verdict_path=None,
        container_env=None,
        backend_name="podman",
        harness_name="claude-code",
    ):
        self.work_dir = Path(work_dir)
        self.run_dir = self.work_dir / "_run"
        self.backend_name = backend_name
        self.harness_name = harness_name
        self.harness = create_harness(harness_name)
        self.default_model = (
            os.environ.get(self.harness.model_env_var()) or self.harness.default_model()
        )
        self.backend = create_backend(
            backend_name,
            harness=self.harness,
            workdir=str(work_dir),
            image=image,
            extra_env=container_env or {},
        )
        if verdict_path is not None:
            self.backend.verdict_path = verdict_path
        self._otel_proc = None
        self.otel_port = None
        self._otel_log = None
        self.traceparent = None
        self.trace_id = None
        self.span_id = None
        self._start_ns = None
        self.last_model = self.default_model
        self.last_effort: str | None = None
        self.last_subagent_effort: str | None = None
        self.last_rc = 1

    def __enter__(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.harness.supports_otel:
            try:
                self._otel_proc, self.otel_port, self._otel_log, _ = start_collector(
                    str(self.run_dir), bind_addr=self.backend.collector_bind_address
                )
                self.trace_id, self.span_id, self.traceparent = generate_trace_context()
            except Exception:
                log.warning("Failed to start OTEL collector, continuing without telemetry")
        try:
            self.backend.setup(otel_port=self.otel_port)
        except BaseException:
            # __exit__ does not run when __enter__ raises, so release the
            # collector and any half-started backend here.
            if self._otel_proc:
                stop_collector(self._otel_proc)
                self._otel_proc = None
            self.backend.stop()
            raise
        self._start_ns = time.time_ns()
        return self

    def run(
        self, prompt, *, model, effort=None, extra_args=None, output_file=None, completion_file=None
    ):
        """Run the agent once with *model* and *effort*; returns the exit code.

        ``effort=None`` resolves to the harness effort env var or the registry
        ``default_effort`` (see ``Harness.resolve_efforts``).

        ``completion_file`` replaces the session's verdict path for this run
        only. When the stream processor saw the run finish but the process had
        to be terminated (for example Codex lingering after its last event), the
        backend promotes that exit code to 0 only if this file exists. The
        classifier uses ``_run/route.json`` here, since it never writes the
        skill's verdict file.
        """
        if completion_file is None:
            return self._run_once(prompt, model, effort, extra_args, output_file)
        saved = self.backend.verdict_path
        self.backend.verdict_path = completion_file
        try:
            return self._run_once(prompt, model, effort, extra_args, output_file)
        finally:
            self.backend.verdict_path = saved

    def _run_once(self, prompt, model, effort, extra_args, output_file):
        self.backend.output_file = output_file
        main_effort, subagent_effort = self.harness.resolve_efforts(effort)
        args = [
            *self.harness.build_effort_args(main_effort, subagent_effort),
            *(extra_args or []),
        ]
        log.info("Model: %s, reasoning effort: %s", model, main_effort if main_effort else "none")
        if subagent_effort is not None:
            log.info("Sub-agent reasoning effort: %s", subagent_effort)
        self.last_model = model
        self.last_effort = main_effort
        self.last_subagent_effort = subagent_effort
        self.last_rc = 1
        self.last_rc = self.backend.run(
            prompt,
            model=model,
            effort=main_effort,
            otel_port=self.otel_port,
            traceparent=self.traceparent,
            extra_args=args or None,
        )
        return self.last_rc

    def __exit__(self, exc_type, exc, tb):
        end_ns = time.time_ns()
        if self._otel_proc:
            stop_collector(self._otel_proc)
            if self._start_ns and self._otel_log:
                try:
                    injected = inject_root_spans(
                        self._otel_log,
                        self._start_ns,
                        end_ns,
                        self.last_rc,
                        fallback_trace_id=self.trace_id,
                        fallback_span_id=self.span_id,
                        attributes=root_span_attributes(
                            self.backend_name,
                            self.harness_name,
                            self.last_model,
                            self.last_effort,
                            self.last_subagent_effort,
                        ),
                    )
                    if injected:
                        log.info("Injected %d synthetic root span(s)", injected)
                except Exception as exc_:
                    print(
                        f"Root span injection failed (non-fatal): {exc_}",
                        file=sys.stderr,
                    )
        self.backend.stop()
        return False


def _default_run_container(
    work_dir,
    prompt,
    output_file,
    *,
    image=None,
    verdict_path=None,
    container_env=None,
    backend_name="podman",
    harness_name="claude-code",
    model=None,
    effort=None,
    router=None,
):
    """Default container runner using the configured backend.

    *model* defaults to the harness env var or default model. *router*, when
    given, is called as ``router(session, prompt)`` before the main run and
    must return a :class:`~agentic_ci.routing.RouteDecision` whose model and
    effort are then used for the run.
    """
    with _AgentSession(
        work_dir,
        image=image,
        verdict_path=verdict_path,
        container_env=container_env,
        backend_name=backend_name,
        harness_name=harness_name,
    ) as session:
        target_model = model or session.default_model
        target_effort = effort
        if router is not None:
            decision = router(session, prompt)
            target_model, target_effort = decision.model, decision.effort
        return session.run(
            prompt, model=target_model, effort=target_effort, output_file=output_file
        )


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

    if config.extra_skills:
        raw_ctx_dir = work_dir / config.context_dir
        if raw_ctx_dir.is_symlink():
            raise ValueError(f"context_dir is a symlink: {raw_ctx_dir}")
        ctx_dir = raw_ctx_dir.resolve()
        try:
            ctx_dir.relative_to(work_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"context_dir escapes work_dir: {config.context_dir!r}") from exc
        ctx_dir.mkdir(parents=True, exist_ok=True)
        config_path = ctx_dir / "config.json"
        if config_path.is_symlink():
            raise ValueError(f"config.json is a symlink: {config_path}")
        config_path.write_text(
            json.dumps(
                {"extra_skills": config.extra_skills},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
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
    output_file = work_dir / "agent-output.txt"

    runner = config.container_runner or _default_run_container
    runner_kwargs: dict = {"image": config.container_image}
    if config.container_env:
        runner_kwargs["container_env"] = config.container_env
    if config.container_runner is None:
        runner_kwargs["verdict_path"] = config.verdict_path_fn(work_dir)
        runner_kwargs["backend_name"] = config.backend_name
        runner_kwargs["harness_name"] = config.harness_name

    if dry_run:
        if dry_run_verdict_path:
            verdict_dest = config.verdict_path_fn(work_dir)
            verdict_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dry_run_verdict_path, verdict_dest)
        rc = 0
    else:
        rc = runner(work_dir, prompt, output_file, **runner_kwargs)

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
            rc = runner(work_dir, prompt, output_file, **runner_kwargs)

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
                rc = runner(work_dir, prompt, output_file, **runner_kwargs)
                if rc != 0:
                    log.error("[%s] Retry container also failed (exit %d)", ticket_key, rc)
                    config.label_applier(
                        ticket_key=ticket_key,
                        verdict=None,
                        rc=rc,
                        mode=mode,
                        work_dir=work_dir,
                        **extra_kwargs,
                    )
                    return rc
                try:
                    verdict = config.verdict_loader(work_dir)
                    verdict_error = None
                except Exception as retry_exc:
                    log.error("[%s] Verdict still missing after retry: %s", ticket_key, retry_exc)
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

    label_rc = config.label_applier(
        ticket_key=ticket_key,
        verdict=verdict,
        mode=mode,
        work_dir=work_dir,
        **extra_kwargs,
    )

    if label_rc:
        log.error(
            "[%s] label_applier returned non-zero exit code: %d",
            ticket_key,
            label_rc,
        )
        return label_rc

    log.info(
        "[%s] %s complete: verdict=%s",
        ticket_key,
        config.skill_name,
        verdict.get("verdict", "unknown"),
    )
    return 0


def _emit_route_event(session, config, ticket_key, decision):
    """Record the routing decision as a ``skill.routed`` telemetry event (best effort)."""
    payload = {
        "event_type": "skill.routed",
        "skill_name": config.skill_name,
        "ticket_key": ticket_key,
        "harness": config.harness_name,
        "backend": config.backend_name,
        "default_model": session.default_model,
        "tier": decision.tier,
        "model": decision.model,
        "effort": decision.effort,
        "source": decision.source,
    }
    try:
        emit_event(
            payload,
            log_root=session.run_dir,
            correlation={"trace_id": session.trace_id} if session.trace_id else None,
            parent_span_id=session.span_id,
        )
    except Exception as exc:
        log.warning("skill.routed event not recorded: %s", exc)


def run_routed_skill(
    config: SkillConfig,
    ticket_key: str,
    work_dir: Path,
    config_dir: Path,
    *,
    mode: str = "resolve",
    ticket: dict | None = None,
    dry_run: bool = False,
    dry_run_verdict_path: Path | None = None,
    classifier_max_turns: int = DEFAULT_CLASSIFIER_MAX_TURNS,
    classifier_prompt_builder: Callable[[str], str] | None = None,
    force_tier: str | None = None,
    **extra_kwargs,
) -> RoutedSkillResult:
    """Run a skill with difficulty-based model routing.

    Behaves like :func:`run_skill` with one addition: before the skill runs,
    a classifier invocation on the harness default model (``CLAUDE_MODEL``,
    ``OPENCODE_MODEL`` or ``CODEX_MODEL``, else ``Harness.default_model()``)
    rates the task as ``low``, ``medium`` or ``high`` and writes
    ``_run/route.json``. The skill then runs on the matching
    :class:`~agentic_ci.routing.ModelTier` from the harness defaults,
    overridden per key by ``config.model_tiers``.

    The classifier runs inside the same sandbox as the skill (same container,
    credentials and network policy). Its raw stream is written to
    ``_run/classifier-output.txt``. Any classifier failure (non-zero exit,
    exception, missing or invalid route file) logs a warning and falls back to
    the default model at the harness's effective default effort (env var or
    registry ``default_effort``), which is exactly what :func:`run_skill`
    would do. Only configuration errors raise, and they
    raise before any container starts.

    The decision is made once per call and reused by every retry that
    :func:`run_skill` performs, so all attempts use the same model. A
    ``skill.routed`` event is appended to the run's ``_run/claude-otel.jsonl``.

    Args:
        classifier_max_turns: turn cap for the classifier where the CLI
            supports one (Claude Code ``--max-turns``).
        classifier_prompt_builder: replaces the default classifier prompt;
            receives the skill prompt and returns the classifier prompt.
        force_tier: skip the classifier and pin this tier.

    ``config.container_runner`` must be ``None``; custom runners have no
    model surface. ``extension_config_writer`` receives a copy of *config*
    whose ``container_runner`` is the routing runner.

    Returns:
        :class:`RoutedSkillResult` with the exit code and the decision
        (``None`` when no agent ran, e.g. ``dry_run`` or a pre-gate block).
    """
    if config.container_runner is not None:
        raise ValueError("run_routed_skill requires the default container runner")
    harness = create_harness(config.harness_name)
    tiers = resolve_model_tiers(harness, config.model_tiers)
    if force_tier is not None and force_tier not in tiers:
        raise ValueError(f"Unknown force_tier {force_tier!r}; expected one of {sorted(tiers)}")

    state: dict[str, RouteDecision] = {}

    def _router(session, prompt):
        if "decision" not in state:
            if force_tier is not None:
                decision = forced_route(force_tier, tiers)
            else:
                decision = classify(
                    # The classifier's evidence of completion is its route file,
                    # not the skill verdict.
                    functools.partial(session.run, completion_file=route_path(work_dir)),
                    work_dir=work_dir,
                    task_prompt=prompt,
                    tiers=tiers,
                    classifier_model=session.default_model,
                    classifier_effort=session.harness.classifier_effort(),
                    classifier_args=session.harness.build_classifier_args(classifier_max_turns),
                    fallback=ModelTier(session.default_model, session.harness.resolve_efforts()[0]),
                    prompt_builder=classifier_prompt_builder,
                )
            state["decision"] = decision
            _emit_route_event(session, config, ticket_key, decision)
        return state["decision"]

    runner = functools.partial(
        _default_run_container,
        verdict_path=config.verdict_path_fn(work_dir),
        backend_name=config.backend_name,
        harness_name=config.harness_name,
        router=_router,
    )
    rc = run_skill(
        dataclasses.replace(config, container_runner=runner),
        ticket_key,
        work_dir,
        config_dir,
        mode=mode,
        ticket=ticket,
        dry_run=dry_run,
        dry_run_verdict_path=dry_run_verdict_path,
        **extra_kwargs,
    )
    return RoutedSkillResult(rc=rc, route=state.get("decision"))
