"""Regression tests for harness-specific telemetry in the routed E2E driver."""

import json
import sys

import pytest

from agentic_ci.otel import inject_root_spans, otel_log_filename, root_span_attributes
from agentic_ci.routing import RouteDecision
from agentic_ci.skill import RoutedSkillResult
from agentic_ci.telemetry import emit_event
from tests.e2e import routed_skill_driver


@pytest.mark.parametrize("harness_name", ["claude-code", "opencode", "codex"])
def test_routed_driver_reads_harness_artifact(monkeypatch, tmp_path, capsys, harness_name):
    run_dir = tmp_path / "_run"
    run_dir.mkdir()
    trace_id = "0123456789abcdef0123456789abcdef"
    span_id = "fedcba9876543210"
    decision = RouteDecision(model="test-model", effort="high", tier="high", source="classifier")
    artifact = run_dir / otel_log_filename(harness_name)
    emit_event(
        {"event_type": "skill.routed", "model": decision.model, "source": decision.source},
        log_root=run_dir,
        log_filename=artifact.name,
        correlation={"trace_id": trace_id},
        parent_span_id=span_id,
    )
    inject_root_spans(
        artifact,
        1,
        2,
        0,
        fallback_trace_id=trace_id,
        fallback_span_id=span_id,
        attributes=root_span_attributes("local", harness_name, decision.model, "high"),
    )
    (tmp_path / "verdict.json").write_text('{"verdict": "committed"}')
    (tmp_path / "agent-output.txt").write_text("pong\n")
    (run_dir / "route.json").write_text(json.dumps({"difficulty": "high", "reason": "test"}))
    (run_dir / "classifier-output.txt").write_text("classified\n")
    monkeypatch.setattr(
        routed_skill_driver,
        "run_routed_skill",
        lambda *args, **kwargs: RoutedSkillResult(rc=0, route=decision),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["driver", "--backend", "local", "--harness", harness_name, "--workdir", str(tmp_path)],
    )

    assert routed_skill_driver.main() == 0
    output = capsys.readouterr().out
    for check in (
        "routed_event",
        "routed_event_parented",
        "routed_event_matches",
        "root_span_present",
        "root_span_model",
    ):
        assert f"ROUTED_CHECK {check}=ok" in output
    assert "=fail" not in output
    if harness_name == "opencode":
        assert not (run_dir / "claude-otel.jsonl").exists()
