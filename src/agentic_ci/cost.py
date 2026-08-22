"""Model pricing helpers for telemetry-derived cost estimates."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

_CUSTOM_COST_MAP_ENV = "AGENTIC_CI_LITELLM_COST_MAP"
_LARGE_CONTEXT_THRESHOLD = 272_000
_BUNDLED_COST_MAP = "data/litellm_openai_cost_map.json"


def _as_rate(value: Any) -> float | None:
    """Convert a LiteLLM map value to a non-negative rate."""
    if isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if rate >= 0 else None


@lru_cache(maxsize=1)
def _load_bundled_map() -> Mapping[str, Any]:
    """Load the generated LiteLLM OpenAI pricing snapshot."""
    try:
        document = json.loads(files("agentic_ci").joinpath(_BUNDLED_COST_MAP).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    models = document.get("models") if isinstance(document, dict) else None
    return models if isinstance(models, dict) else {}


@lru_cache(maxsize=4)
def _load_custom_map(path: str | None) -> Mapping[str, Any]:
    """Load an optional LiteLLM-format model cost map."""
    if not path:
        return {}
    try:
        with Path(path).open(encoding="utf-8") as cost_file:
            loaded = json.load(cost_file)
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _model_candidates(model: str) -> list[str]:
    """Return likely LiteLLM keys for a Codex model name."""
    candidates = [model]
    if model.startswith("openai/"):
        candidates.append(model.removeprefix("openai/"))
    else:
        candidates.append(f"openai/{model}")
    if model.startswith("chatgpt/"):
        candidates.append(model.removeprefix("chatgpt/"))
    return list(dict.fromkeys(candidates))


def _get_model_pricing(model: str) -> Mapping[str, Any] | None:
    """Resolve a model from the optional map, then the bundled snapshot."""
    custom_map = _load_custom_map(os.environ.get(_CUSTOM_COST_MAP_ENV))
    for candidate in _model_candidates(model):
        entry = custom_map.get(candidate)
        if isinstance(entry, dict):
            return entry

    bundled_map = _load_bundled_map()
    for candidate in _model_candidates(model):
        entry = bundled_map.get(candidate)
        if isinstance(entry, dict):
            return entry
    return None


def _rate(
    pricing: Mapping[str, Any],
    field: str,
    service_tier: str | None,
    prompt_tokens: float,
) -> float | None:
    """Select a standard, service-tier, and context-length-specific rate."""
    tier = service_tier.lower() if service_tier else ""
    tier_suffix = f"_{tier}" if tier in ("priority", "flex") else ""
    large_suffix = "_above_272k_tokens" if prompt_tokens > _LARGE_CONTEXT_THRESHOLD else ""

    for name in (
        f"{field}{large_suffix}{tier_suffix}",
        f"{field}{tier_suffix}",
        f"{field}{large_suffix}",
        field,
    ):
        rate = _as_rate(pricing.get(name))
        if rate is not None:
            return rate
    return None


def estimate_cost(
    model: str,
    input_tokens: float,
    output_tokens: float,
    cached_input_tokens: float = 0,
    cache_creation_input_tokens: float = 0,
    service_tier: str | None = None,
) -> float | None:
    """Estimate USD cost from Codex response usage.

    ``input_tokens`` is the fresh input count. Codex's response event reports
    the inclusive input count separately; callers should subtract cache-read
    and cache-write counts before passing it here. ``None`` means the model has
    no known price in the configured or bundled LiteLLM map.
    """
    counts = (input_tokens, output_tokens, cached_input_tokens, cache_creation_input_tokens)
    if any(value < 0 for value in counts):
        return None

    pricing = _get_model_pricing(model)
    if pricing is None:
        return None

    prompt_tokens = input_tokens + cached_input_tokens + cache_creation_input_tokens
    input_rate = _rate(pricing, "input_cost_per_token", service_tier, prompt_tokens)
    output_rate = _rate(pricing, "output_cost_per_token", service_tier, prompt_tokens)
    cache_read_rate = _rate(pricing, "cache_read_input_token_cost", service_tier, prompt_tokens)
    cache_creation_rate = _rate(
        pricing,
        "cache_creation_input_token_cost",
        service_tier,
        prompt_tokens,
    )

    if input_tokens and input_rate is None:
        return None
    if output_tokens and output_rate is None:
        return None
    if cached_input_tokens and cache_read_rate is None:
        return None
    if cache_creation_input_tokens and cache_creation_rate is None:
        return None

    return sum(
        (
            input_tokens * (input_rate or 0),
            output_tokens * (output_rate or 0),
            cached_input_tokens * (cache_read_rate or 0),
            cache_creation_input_tokens * (cache_creation_rate or 0),
        )
    )
