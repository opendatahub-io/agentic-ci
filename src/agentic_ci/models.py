"""Model registry: the one place to bump model ids and effort levels.

Every harness reads its default model, its default reasoning effort, its
``low``/``medium``/``high`` routing tiers, the reasoning-effort values its
CLI accepts, and the effort used for the difficulty classifier from
:data:`MODEL_REGISTRY`. The harness
classes only know how to turn an effort value into a CLI flag. To move a
harness to a new model or effort set, edit its :class:`HarnessModels` entry
here and the matching table in ``README.md``; nothing else needs to change.

Keys are the ``--harness`` names accepted by ``create_harness()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelTier:
    """One routing tier: a model id plus the default reasoning effort for it.

    ``effort`` is harness-specific (see :attr:`HarnessModels.efforts`).
    ``None`` means "resolve normally": the effort env var, else the registry
    ``default_effort``. Only the literal override value ``none`` (via
    ``--effort`` or the env var) disables the effort flag.
    """

    model: str
    effort: str | None = None


@dataclass(frozen=True)
class HarnessModels:
    """Model ids and effort levels for one harness.

    ``default`` is used when no ``--model`` flag or model env var is set and
    is also the classifier model for routed runs. ``tiers`` maps ``low``,
    ``medium`` and ``high`` to a :class:`ModelTier`; ``high`` should reuse
    ``default`` so routed runs never regress hard tasks. ``efforts`` is the
    set of effort values the harness CLI accepts; ``default_effort``, every
    tier effort, ``subagent_effort`` and ``classifier_effort`` must be in it
    (or ``None``).

    ``default_effort`` is the reasoning effort passed on every run when no
    ``--effort`` flag, effort env var, or tier effort says otherwise. A
    ``None`` effort anywhere (tier, classifier) means "use the default".
    ``subagent_effort`` applies to agents the harness spawns, where the CLI
    has a separate knob (Codex); ``None`` follows the main effort.
    """

    default: str
    tiers: dict[str, ModelTier] = field(default_factory=dict)
    efforts: frozenset[str] = frozenset()
    default_effort: str | None = "high"
    subagent_effort: str | None = None
    classifier_effort: str | None = None


MODEL_REGISTRY: dict[str, HarnessModels] = {
    # Claude Code: ``claude --effort`` accepts low/medium/high/xhigh/max.
    "claude-code": HarnessModels(
        default="claude-opus-4-6",
        tiers={
            "low": ModelTier("claude-sonnet-4-5", "medium"),
            "medium": ModelTier("claude-sonnet-4-5", "high"),
            "high": ModelTier("claude-opus-4-6", "high"),
        },
        efforts=frozenset({"low", "medium", "high", "xhigh", "max"}),
        default_effort="high",
        classifier_effort="low",
    ),
    # OpenCode: ``opencode run --variant`` selects a per-model variant
    # (opencode provider/transform.ts, verified against v1.18.25). Claude 4.6
    # ids on the anthropic and google-vertex providers get adaptive-thinking
    # variants low/medium/high/max; older ids such as claude-sonnet-4-5 only
    # get the thinking-budget variants high and max, so the default tiers
    # use no variant or "high" on sonnet 4.5.
    "opencode": HarnessModels(
        default="google-vertex/claude-opus-4-6@default",
        tiers={
            "low": ModelTier("google-vertex/claude-sonnet-4-5@20250929", None),
            "medium": ModelTier("google-vertex/claude-sonnet-4-5@20250929", "high"),
            "high": ModelTier("google-vertex/claude-opus-4-6@default", "high"),
        },
        efforts=frozenset({"low", "medium", "high", "max"}),
        default_effort="high",
        classifier_effort=None,
    ),
    # Codex: ``codex -c model_reasoning_effort=`` accepts
    # minimal/low/medium/high/xhigh. Codex does not reject unknown values,
    # so ``efforts`` is the gate. Spawned sub-agents get
    # ``agents.default_subagent_reasoning_effort`` (``subagent_effort``,
    # following the main effort when None); without it they run at the model
    # default, which is low for gpt-5.6-sol (RHAIFIRST-649).
    "codex": HarnessModels(
        default="gpt-6-sol",
        tiers={
            "low": ModelTier("gpt-6-luna", "xhigh"),
            "medium": ModelTier("gpt-6-luna", "xhigh"),
            "high": ModelTier("gpt-6-sol", "high"),
        },
        efforts=frozenset({"minimal", "low", "medium", "high", "xhigh"}),
        default_effort="high",
        subagent_effort=None,
        classifier_effort="low",
    ),
}


def harness_models(name: str) -> HarnessModels:
    """Return the registry entry for harness *name*.

    Raises ``ValueError`` for a harness that has no entry.
    """
    try:
        return MODEL_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"No model registry entry for harness {name!r}; known: {sorted(MODEL_REGISTRY)}"
        ) from None
