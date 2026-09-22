"""Tests for telemetry-derived model cost estimates."""

import json
from importlib.resources import files

import pytest

from agentic_ci.cost import estimate_cost
from agentic_ci.models import MODEL_REGISTRY


def test_bundled_cost_map_has_litellm_provenance():
    snapshot = json.loads(
        files("agentic_ci").joinpath("data/litellm_openai_cost_map.json").read_text()
    )

    assert len(snapshot["source"]["commit"]) == 40
    assert snapshot["source"]["url"].endswith(
        f"/{snapshot['source']['commit']}/model_prices_and_context_window.json"
    )
    assert len(snapshot["models"]) >= 100


def test_estimate_codex_priority_cost_with_cache_rates():
    cost = estimate_cost(
        "gpt-5.6-sol",
        input_tokens=50,
        output_tokens=20,
        cached_input_tokens=40,
        cache_creation_input_tokens=10,
        service_tier="priority",
    )

    assert cost == pytest.approx(0.001332)


@pytest.mark.parametrize(
    "model",
    sorted(
        {MODEL_REGISTRY["codex"].default}
        | {tier.model for tier in MODEL_REGISTRY["codex"].tiers.values()}
    ),
)
def test_codex_registry_models_are_priced(model):
    assert estimate_cost(model, input_tokens=1, output_tokens=1) is not None


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # 100K input, 100K output, 50K cache reads, 50K cache writes at the
        # published Standard per-1M rates (input, output, cached, cache write).
        ("gpt-6-sol", 0.1 * 2 + 0.1 * 10 + 0.05 * 0.2 + 0.05 * 2.5),
        ("gpt-6-luna", 0.1 * 0.1 + 0.1 * 0.5 + 0.05 * 0.01 + 0.05 * 0.125),
    ],
)
def test_estimate_gpt_6_standard_cost(model, expected):
    cost = estimate_cost(
        model,
        input_tokens=100_000,
        output_tokens=100_000,
        cached_input_tokens=50_000,
        cache_creation_input_tokens=50_000,
    )

    assert cost == pytest.approx(expected)


def test_estimate_cost_uses_custom_litellm_map(tmp_path, monkeypatch):
    cost_map = tmp_path / "model-prices.json"
    cost_map.write_text(
        json.dumps(
            {
                "custom-model": {
                    "input_cost_per_token": 1e-6,
                    "output_cost_per_token": 2e-6,
                    "cache_read_input_token_cost": 3e-7,
                    "cache_creation_input_token_cost": 4e-7,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTIC_CI_LITELLM_COST_MAP", str(cost_map))

    cost = estimate_cost(
        "custom-model",
        input_tokens=10,
        output_tokens=20,
        cached_input_tokens=3,
        cache_creation_input_tokens=4,
    )

    assert cost == pytest.approx(0.0000525)


def test_estimate_cost_returns_none_for_unknown_model():
    assert estimate_cost("unknown-model", input_tokens=10, output_tokens=10) is None


def test_estimate_cost_returns_none_for_negative_usage():
    assert estimate_cost("gpt-5.6-sol", input_tokens=-1, output_tokens=10) is None
