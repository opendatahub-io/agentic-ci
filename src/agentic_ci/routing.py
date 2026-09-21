"""Difficulty-based model routing for skill runs.

The router asks an agent (running on the harness default model, inside the
same sandbox as the real run) to rate a task as ``low``, ``medium`` or
``high`` difficulty and to write that rating to ``_run/route.json``. The
host then maps the rating to a :class:`ModelTier` (a model id plus a default
reasoning effort) and runs the actual skill on it.

This module is pure: it knows nothing about backends or containers. The
caller supplies a :class:`RunCallable` that performs one agent invocation,
which keeps :func:`classify` testable without a sandbox.

Every classifier failure falls back to the caller-supplied default tier, so
routing can never make a run worse than an unrouted one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agentic_ci.harness import Harness

log = logging.getLogger(__name__)

TIER_NAMES: tuple[str, ...] = ("low", "medium", "high")
"""Difficulty tiers, from cheapest to strongest."""

DEFAULT_CLASSIFIER_MAX_TURNS = 10
"""Default turn cap for the classifier run (only enforced where the CLI supports it)."""

ROUTE_FILENAME = "route.json"
"""File the classifier writes under ``<work_dir>/_run/``."""

CLASSIFIER_OUTPUT_FILENAME = "classifier-output.txt"
"""Raw classifier stream, written under ``<work_dir>/_run/``."""

_REASON_MAX_LENGTH = 300

CLASSIFIER_PROMPT_TEMPLATE = """\
You are a triage step that rates how difficult a task is for an autonomous \
coding agent. Do NOT perform the task. Do NOT modify any file other than the \
route file named below. Do NOT run commands that change state (no git commit, \
no package installs, no builds).

You may read files and directories (including the .context/ directory, if \
present) to gauge the scope of the task. Read at most {max_files} files.

Rate the task with exactly one of these difficulty levels:

- low: a localized change with a clear specification, touching one or two \
files, no design decisions, no investigation needed.
- medium: a multi-file change with a clear specification, or a change that \
needs moderate investigation to locate the right place.
- high: ambiguous requirements, a cross-cutting refactor, an unfamiliar \
subsystem, or debugging with an unclear root cause.

When you have decided, write exactly this JSON (and nothing else) to the file \
`{route_file}` relative to the current working directory, then stop:

{{"difficulty": "<low|medium|high>", "reason": "<one sentence>"}}

--- TASK TO RATE ---
{task_prompt}
--- END TASK ---
"""


@dataclass(frozen=True)
class ModelTier:
    """One routing tier: a model id plus the default reasoning effort for it.

    ``effort`` is harness-specific (see ``Harness.build_effort_args``) and
    ``None`` means "do not pass an effort flag".
    """

    model: str
    effort: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """Outcome of routing one skill run.

    ``source`` is ``"classifier"`` when the agent's rating was used,
    ``"forced"`` when the caller pinned a tier, and ``"fallback"`` when the
    classifier failed and the default model was used instead. ``tier`` is
    ``None`` on fallback.
    """

    model: str
    effort: str | None
    tier: str | None
    source: str
    reason: str = ""

    def to_dict(self) -> dict:
        """Return a plain dict (for telemetry and consumers)."""
        return asdict(self)


class RouteError(ValueError):
    """Raised when the classifier's route file is missing, malformed, or invalid."""


class RunCallable(Protocol):
    """One agent invocation inside an already-prepared sandbox."""

    def __call__(
        self,
        prompt: str,
        *,
        model: str,
        effort: str | None = None,
        extra_args: list[str] | None = None,
        output_file: Path | None = None,
    ) -> int: ...


def route_path(work_dir: Path) -> Path:
    """Return the path of the classifier's route file for *work_dir*."""
    return Path(work_dir) / "_run" / ROUTE_FILENAME


def resolve_model_tiers(
    harness: Harness, overrides: Mapping[str, ModelTier]
) -> dict[str, ModelTier]:
    """Overlay *overrides* on the harness default tier map and validate it.

    Raises ``ValueError`` for an unknown tier name, an empty model id, or an
    effort value the harness rejects, so configuration errors surface before
    any container is started.
    """
    tiers = dict(harness.default_model_tiers())
    for name, tier in overrides.items():
        if name not in TIER_NAMES:
            raise ValueError(f"Unknown model tier {name!r}; expected one of {TIER_NAMES}")
        tiers[name] = tier
    for name in TIER_NAMES:
        tier = tiers.get(name)
        if tier is None:
            raise ValueError(f"Harness {harness.name} defines no {name!r} model tier")
        if not tier.model or not tier.model.strip():
            raise ValueError(f"Model tier {name!r} has an empty model id")
        # Validates the effort value for this harness; raises ValueError when invalid.
        harness.build_effort_args(tier.effort)
    return tiers


def build_classifier_prompt(
    task_prompt: str,
    *,
    route_file: str = f"_run/{ROUTE_FILENAME}",
    max_files: int = DEFAULT_CLASSIFIER_MAX_TURNS,
) -> str:
    """Build the prompt that asks the agent to rate *task_prompt*."""
    return CLASSIFIER_PROMPT_TEMPLATE.format(
        task_prompt=task_prompt, route_file=route_file, max_files=max_files
    )


def load_route(path: Path) -> tuple[str, str]:
    """Read the classifier's route file and return ``(difficulty, reason)``.

    Raises :class:`RouteError` when the file is missing, is a symlink, is not
    a JSON object, or names a difficulty outside :data:`TIER_NAMES`.
    """
    path = Path(path)
    if path.is_symlink():
        raise RouteError(f"route file is a symlink: {path}")
    if not path.is_file():
        raise RouteError(f"route file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RouteError(f"route file unreadable or not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RouteError("route file must contain a JSON object")
    difficulty = str(data.get("difficulty", "")).strip().lower()
    if difficulty not in TIER_NAMES:
        raise RouteError(f"route file difficulty {data.get('difficulty')!r} not in {TIER_NAMES}")
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        reason = ""
    return difficulty, reason[:_REASON_MAX_LENGTH]


def forced_route(tier: str, tiers: Mapping[str, ModelTier]) -> RouteDecision:
    """Return a decision that pins *tier* without running the classifier."""
    if tier not in tiers:
        raise ValueError(f"Unknown model tier {tier!r}; expected one of {TIER_NAMES}")
    chosen = tiers[tier]
    return RouteDecision(
        model=chosen.model,
        effort=chosen.effort,
        tier=tier,
        source="forced",
        reason="tier forced by caller",
    )


def classify(
    run: RunCallable,
    *,
    work_dir: Path,
    task_prompt: str,
    tiers: Mapping[str, ModelTier],
    classifier_model: str,
    classifier_effort: str | None,
    classifier_args: list[str],
    fallback: ModelTier,
    prompt_builder: Callable[[str], str] | None = None,
) -> RouteDecision:
    """Run the classifier and map its rating to a :class:`RouteDecision`.

    *run* performs one agent invocation. The classifier prompt is built by
    *prompt_builder* (default :func:`build_classifier_prompt`) from
    *task_prompt*. Any failure (exception, non-zero exit, missing or invalid
    route file) logs a warning and returns a ``"fallback"`` decision using
    *fallback*; this function never raises on classifier failure.
    """
    work_dir = Path(work_dir)
    run_dir = work_dir / "_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    route_file = route_path(work_dir)
    if route_file.is_symlink() or route_file.exists():
        route_file.unlink()

    prompt = (prompt_builder or build_classifier_prompt)(task_prompt)
    output_file = run_dir / CLASSIFIER_OUTPUT_FILENAME

    failure: str | None = None
    try:
        rc = run(
            prompt,
            model=classifier_model,
            effort=classifier_effort,
            extra_args=list(classifier_args) or None,
            output_file=output_file,
        )
    except Exception as exc:
        failure = f"classifier raised: {exc}"
    else:
        if rc != 0:
            failure = f"classifier exited with code {rc}"

    if failure is None:
        try:
            difficulty, reason = load_route(route_file)
        except RouteError as exc:
            failure = str(exc)
        else:
            chosen = tiers[difficulty]
            log.info(
                "Routed to tier %s (model=%s, effort=%s): %s",
                difficulty,
                chosen.model,
                chosen.effort,
                reason,
            )
            return RouteDecision(
                model=chosen.model,
                effort=chosen.effort,
                tier=difficulty,
                source="classifier",
                reason=reason,
            )

    log.warning("Model routing fell back to %s: %s", fallback.model, failure)
    return RouteDecision(
        model=fallback.model,
        effort=fallback.effort,
        tier=None,
        source="fallback",
        reason=failure[:_REASON_MAX_LENGTH],
    )
