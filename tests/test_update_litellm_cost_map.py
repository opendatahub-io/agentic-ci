"""Tests for the LiteLLM pricing snapshot generator."""

import pytest

from scripts.update_litellm_cost_map import build_snapshot


def test_build_snapshot_filters_openai_and_preserves_cost_fields():
    source = {
        **{
            f"openai-model-{index}": {
                "litellm_provider": "openai",
                "input_cost_per_token": 1e-6,
            }
            for index in range(100)
        },
        "openai-model-with-alias": {
            "litellm_provider": "openai",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token_priority": 4e-6,
            "aliases": ["openai-model-alias"],
            "max_tokens": 10,
        },
        "anthropic-model": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 3e-6,
        },
    }

    snapshot = build_snapshot(source, "a" * 40)

    assert "anthropic-model" not in snapshot["models"]
    assert snapshot["models"]["openai-model-alias"]["input_cost_per_token"] == 2e-6
    assert snapshot["models"]["openai-model-with-alias"] == {
        "aliases": ["openai-model-alias"],
        "input_cost_per_token": 2e-6,
        "litellm_provider": "openai",
        "output_cost_per_token_priority": 4e-6,
    }
    assert snapshot["source"]["commit"] == "a" * 40


def test_build_snapshot_rejects_too_few_openai_models():
    with pytest.raises(ValueError, match="unexpectedly few"):
        build_snapshot(
            {"model": {"litellm_provider": "openai", "input_cost_per_token": 1e-6}},
            "a" * 40,
        )
