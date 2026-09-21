"""Tests for the model registry that every harness reads from."""

import pytest

from agentic_ci.harness import create_harness
from agentic_ci.models import MODEL_REGISTRY, HarnessModels, ModelTier, harness_models
from agentic_ci.routing import TIER_NAMES

HARNESSES = ("claude-code", "opencode", "codex")


def test_registry_covers_every_harness():
    assert set(MODEL_REGISTRY) == set(HARNESSES)


@pytest.mark.parametrize("name", HARNESSES)
def test_entry_is_consistent(name):
    entry = harness_models(name)
    assert isinstance(entry, HarnessModels)
    assert entry.default
    assert set(entry.tiers) == set(TIER_NAMES)
    for tier_name, tier in entry.tiers.items():
        assert isinstance(tier, ModelTier)
        assert tier.model, tier_name
        assert tier.effort is None or tier.effort in entry.efforts, tier_name
    assert entry.tiers["high"].model == entry.default
    assert entry.classifier_effort is None or entry.classifier_effort in entry.efforts


@pytest.mark.parametrize("name", HARNESSES)
def test_harness_reads_from_registry(name):
    harness = create_harness(name)
    entry = harness_models(name)
    assert harness.registry_key == name
    assert harness.models is entry
    assert harness.default_model() == entry.default
    assert harness.default_model_tiers() == entry.tiers
    assert harness.default_model_tiers() is not entry.tiers
    assert harness.classifier_effort() == entry.classifier_effort
    for effort in entry.efforts:
        assert harness.build_effort_args(effort) == harness.effort_args(effort)


def test_unknown_harness_raises():
    with pytest.raises(ValueError, match="No model registry entry"):
        harness_models("gemini")
