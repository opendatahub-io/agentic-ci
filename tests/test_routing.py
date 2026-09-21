"""Tests for the pure routing module (no containers involved)."""

import json

import pytest

from agentic_ci.harness import ClaudeCodeHarness
from agentic_ci.routing import (
    DEFAULT_CLASSIFIER_MAX_TURNS,
    TIER_NAMES,
    ModelTier,
    RouteDecision,
    RouteError,
    build_classifier_prompt,
    classify,
    forced_route,
    load_route,
    resolve_model_tiers,
    route_path,
)


class StubHarness:
    """Minimal harness double exposing only what routing needs."""

    name = "Stub"

    def default_model_tiers(self):
        return {
            "low": ModelTier("stub-small", "low"),
            "medium": ModelTier("stub-mid", None),
            "high": ModelTier("stub-big", "high"),
        }

    def build_effort_args(self, effort):
        if effort is None:
            return []
        if effort not in {"low", "high"}:
            raise ValueError(f"bad effort {effort}")
        return ["--effort", effort]


TIERS = StubHarness().default_model_tiers()
FALLBACK = ModelTier("stub-default", None)


class TestResolveModelTiers:
    def test_defaults_when_no_overrides(self):
        assert resolve_model_tiers(StubHarness(), {}) == TIERS

    def test_override_replaces_single_tier(self):
        tiers = resolve_model_tiers(StubHarness(), {"low": ModelTier("custom", "high")})
        assert tiers["low"] == ModelTier("custom", "high")
        assert tiers["medium"] == TIERS["medium"]
        assert tiers["high"] == TIERS["high"]

    def test_unknown_tier_name_raises(self):
        with pytest.raises(ValueError, match="Unknown model tier 'ultra'"):
            resolve_model_tiers(StubHarness(), {"ultra": ModelTier("x")})

    def test_empty_model_raises(self):
        with pytest.raises(ValueError, match="empty model id"):
            resolve_model_tiers(StubHarness(), {"low": ModelTier("  ")})

    def test_invalid_effort_raises_before_run(self):
        with pytest.raises(ValueError, match="bad effort turbo"):
            resolve_model_tiers(StubHarness(), {"high": ModelTier("stub-big", "turbo")})

    def test_real_harness_defaults_validate(self):
        tiers = resolve_model_tiers(ClaudeCodeHarness(), {})
        assert set(tiers) == set(TIER_NAMES)


class TestBuildClassifierPrompt:
    def test_contains_task_route_file_and_budget(self):
        prompt = build_classifier_prompt("Fix the typo in README", max_files=3)
        assert "Fix the typo in README" in prompt
        assert "_run/route.json" in prompt
        assert "Read at most 3 files" in prompt
        assert '"difficulty"' in prompt
        for tier in TIER_NAMES:
            assert f"- {tier}:" in prompt

    def test_default_budget(self):
        prompt = build_classifier_prompt("task")
        assert f"Read at most {DEFAULT_CLASSIFIER_MAX_TURNS} files" in prompt


class TestLoadRoute:
    def test_valid(self, tmp_path):
        path = tmp_path / "route.json"
        path.write_text(json.dumps({"difficulty": "medium", "reason": "two files"}))
        assert load_route(path) == ("medium", "two files")

    def test_normalizes_case_and_whitespace(self, tmp_path):
        path = tmp_path / "route.json"
        path.write_text(json.dumps({"difficulty": "  HIGH \n"}))
        assert load_route(path) == ("high", "")

    def test_missing_file(self, tmp_path):
        with pytest.raises(RouteError, match="not found"):
            load_route(tmp_path / "route.json")

    def test_malformed_json(self, tmp_path):
        path = tmp_path / "route.json"
        path.write_text("{not json")
        with pytest.raises(RouteError, match="not JSON"):
            load_route(path)

    def test_non_object(self, tmp_path):
        path = tmp_path / "route.json"
        path.write_text(json.dumps(["low"]))
        with pytest.raises(RouteError, match="JSON object"):
            load_route(path)

    def test_unknown_difficulty(self, tmp_path):
        path = tmp_path / "route.json"
        path.write_text(json.dumps({"difficulty": "extreme"}))
        with pytest.raises(RouteError, match="'extreme' not in"):
            load_route(path)

    def test_missing_difficulty(self, tmp_path):
        path = tmp_path / "route.json"
        path.write_text(json.dumps({"reason": "no rating"}))
        with pytest.raises(RouteError, match="not in"):
            load_route(path)

    def test_symlink_rejected(self, tmp_path):
        target = tmp_path / "real.json"
        target.write_text(json.dumps({"difficulty": "low"}))
        link = tmp_path / "route.json"
        link.symlink_to(target)
        with pytest.raises(RouteError, match="symlink"):
            load_route(link)

    def test_reason_truncated_and_non_string_dropped(self, tmp_path):
        path = tmp_path / "route.json"
        path.write_text(json.dumps({"difficulty": "low", "reason": "x" * 1000}))
        assert len(load_route(path)[1]) == 300
        path.write_text(json.dumps({"difficulty": "low", "reason": ["not", "a", "string"]}))
        assert load_route(path) == ("low", "")


class TestForcedRoute:
    def test_pins_tier(self):
        decision = forced_route("medium", TIERS)
        assert decision == RouteDecision(
            model="stub-mid",
            effort=None,
            tier="medium",
            source="forced",
            reason="tier forced by caller",
        )

    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError, match="Unknown model tier"):
            forced_route("ultra", TIERS)


class FakeRun:
    """Records classifier invocations and optionally writes the route file."""

    def __init__(self, work_dir, *, difficulty=None, rc=0, raise_exc=None, raw=None):
        self.work_dir = work_dir
        self.difficulty = difficulty
        self.rc = rc
        self.raise_exc = raise_exc
        self.raw = raw
        self.calls = []

    def __call__(self, prompt, *, model, effort=None, extra_args=None, output_file=None):
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "effort": effort,
                "extra_args": extra_args,
                "output_file": output_file,
            }
        )
        if self.raise_exc:
            raise self.raise_exc
        if self.raw is not None:
            route_path(self.work_dir).write_text(self.raw)
        elif self.difficulty is not None:
            route_path(self.work_dir).write_text(
                json.dumps({"difficulty": self.difficulty, "reason": "because"})
            )
        return self.rc


def _classify(run, work_dir, **overrides):
    kwargs = {
        "work_dir": work_dir,
        "task_prompt": "Do the task",
        "tiers": TIERS,
        "classifier_model": "stub-default",
        "classifier_effort": "low",
        "classifier_args": ["--max-turns", "4"],
        "fallback": FALLBACK,
    }
    kwargs.update(overrides)
    return classify(run, **kwargs)


class TestClassify:
    def test_success_maps_difficulty_to_tier(self, tmp_path):
        run = FakeRun(tmp_path, difficulty="high")
        decision = _classify(run, tmp_path)
        assert decision == RouteDecision(
            model="stub-big", effort="high", tier="high", source="classifier", reason="because"
        )

    def test_classifier_invocation_shape(self, tmp_path):
        run = FakeRun(tmp_path, difficulty="low")
        _classify(run, tmp_path)
        call = run.calls[0]
        assert call["model"] == "stub-default"
        assert call["effort"] == "low"
        assert call["extra_args"] == ["--max-turns", "4"]
        assert call["output_file"] == tmp_path / "_run" / "classifier-output.txt"
        assert "Do the task" in call["prompt"]
        assert "_run/route.json" in call["prompt"]

    def test_empty_classifier_args_passed_as_none(self, tmp_path):
        run = FakeRun(tmp_path, difficulty="low")
        _classify(run, tmp_path, classifier_args=[])
        assert run.calls[0]["extra_args"] is None

    def test_custom_prompt_builder(self, tmp_path):
        run = FakeRun(tmp_path, difficulty="low")
        _classify(run, tmp_path, prompt_builder=lambda task: f"RATE: {task}")
        assert run.calls[0]["prompt"] == "RATE: Do the task"

    def test_stale_route_file_removed_before_run(self, tmp_path):
        route_path(tmp_path).parent.mkdir(parents=True)
        route_path(tmp_path).write_text(json.dumps({"difficulty": "high"}))
        run = FakeRun(tmp_path)  # writes nothing
        decision = _classify(run, tmp_path)
        assert decision.source == "fallback"
        assert "not found" in decision.reason

    def test_nonzero_exit_falls_back(self, tmp_path):
        run = FakeRun(tmp_path, difficulty="low", rc=137)
        decision = _classify(run, tmp_path)
        assert decision == RouteDecision(
            model="stub-default",
            effort=None,
            tier=None,
            source="fallback",
            reason="classifier exited with code 137",
        )

    def test_exception_falls_back(self, tmp_path):
        run = FakeRun(tmp_path, raise_exc=RuntimeError("boom"))
        decision = _classify(run, tmp_path)
        assert decision.source == "fallback"
        assert decision.model == "stub-default"
        assert "boom" in decision.reason

    def test_invalid_route_falls_back(self, tmp_path):
        run = FakeRun(tmp_path, raw=json.dumps({"difficulty": "extreme"}))
        decision = _classify(run, tmp_path)
        assert decision.source == "fallback"
        assert "extreme" in decision.reason

    def test_fallback_uses_fallback_effort(self, tmp_path):
        run = FakeRun(tmp_path, rc=1)
        decision = _classify(run, tmp_path, fallback=ModelTier("big", "high"))
        assert (decision.model, decision.effort) == ("big", "high")


def test_route_decision_to_dict():
    decision = RouteDecision(model="m", effort=None, tier="low", source="classifier", reason="r")
    assert decision.to_dict() == {
        "model": "m",
        "effort": None,
        "tier": "low",
        "source": "classifier",
        "reason": "r",
    }
