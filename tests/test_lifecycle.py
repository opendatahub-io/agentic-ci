"""Tests for the generic lifecycle outcome event contract."""

import json

import pytest

from agentic_ci.lifecycle import (
    EVENT_TYPE,
    LifecycleEventError,
    OutcomeCorrelation,
    OutcomeEvent,
    OutcomeMetrics,
    event_id_for,
    is_compatible_schema_version,
    outcome_event_schema,
    validate_outcome_event,
)


def test_event_id_is_stable_for_stage_attempt() -> None:
    first = event_id_for("run-123", "proposed", 2)
    second = event_id_for("run-123", "proposed", 2)

    assert first == second
    assert first != event_id_for("run-123", "proposed", 3)
    assert first != event_id_for("run-123", "merged", 2)
    assert first.startswith("outcome-v1-")


def test_event_is_immutable_and_round_trips() -> None:
    event = OutcomeEvent.create(
        correlation_id="run-123",
        stage="proposed",
        event_timestamp="2026-09-04T12:00:00Z",
        correlation=OutcomeCorrelation(
            jira_key="AIPCC-31384",
            jira_project="AIPCC",
            repository="org/repo",
            branch="feature/outcome",
            forge_url="https://gitlab.example/org/repo/-/merge_requests/7",
            forge_id="7",
            pipeline_id="pipeline-1",
            job_id="job-2",
            trace_id="trace-3",
            session_id="session-4",
        ),
        harness="codex",
        model="gpt-5.6-luna",
        verdict="committed",
        reason="MR opened",
        metrics=OutcomeMetrics(
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=30,
            cache_write_tokens=40,
            total_tokens=100,
            cost_usd=0.5,
            latency_ms=1200,
        ),
        metadata={"owner": "autofix", "labels": ["jira-autofix"]},
    )

    assert event.event_type == EVENT_TYPE
    assert event.stage_timestamp == event.event_timestamp
    assert event.to_dict()["correlation"]["jira_key"] == "AIPCC-31384"
    assert event.to_dict()["metrics"]["cache_read_tokens"] == 30
    assert event.to_dict()["metrics"]["cache_write_tokens"] == 40
    assert validate_outcome_event(event.to_dict()).to_dict() == event.to_dict()
    assert json.loads(event.to_json()) == event.to_dict()

    with pytest.raises((AttributeError, TypeError)):
        event.stage = "merged"  # type: ignore[misc]
    with pytest.raises(TypeError):
        event.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        event.metadata["labels"].append("other")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("stage", "final_outcome"),
    [
        ("eligibility", "eligible"),
        ("analyzed", "analyzed"),
        ("proposed", "proposed"),
        ("iterated", "iterated"),
        ("blocked", "blocked"),
        ("rejected", "rejected"),
        ("abandoned", "abandoned"),
        ("stale", "stale"),
        ("merged", "merged"),
        ("reconciliation", "reconciled"),
    ],
)
def test_all_lifecycle_outcomes_are_supported(stage: str, final_outcome: str) -> None:
    event = OutcomeEvent.create(
        correlation_id="generic-run",
        stage=stage,  # type: ignore[arg-type]
        event_timestamp="2026-09-04T12:00:00+00:00",
    )

    assert event.final_outcome == final_outcome


def test_minor_versions_are_forward_compatible_and_extensions_are_retained() -> None:
    event = OutcomeEvent.create(
        correlation_id="generic-run",
        stage="analyzed",
        event_timestamp="2026-09-04T12:00:00Z",
    )
    payload = event.to_dict()
    payload["schema_version"] = "1.1"
    payload["future_field"] = {"enabled": True}
    payload["event_id"] = event_id_for("generic-run", "analyzed", schema_version="1.1")

    parsed = validate_outcome_event(payload)

    assert is_compatible_schema_version("1.1")
    assert parsed.event_id == event.event_id
    assert parsed.extensions["future_field"] == {"enabled": True}
    assert parsed.to_dict()["future_field"] == {"enabled": True}
    assert not is_compatible_schema_version("2.0")


def test_invalid_events_are_rejected() -> None:
    with pytest.raises(LifecycleEventError, match="Unsupported lifecycle event schema"):
        OutcomeEvent.create(correlation_id="run", stage="analyzed", schema_version="2.0")
    with pytest.raises(LifecycleEventError, match="non-negative"):
        OutcomeEvent.create(
            correlation_id="run",
            stage="analyzed",
            metrics=OutcomeMetrics(input_tokens=-1),
        )
    with pytest.raises(LifecycleEventError, match="timezone"):
        OutcomeEvent.create(
            correlation_id="run",
            stage="analyzed",
            event_timestamp="2026-09-04T12:00:00",
        )
    with pytest.raises(LifecycleEventError, match="unknown lifecycle stage"):
        event_id_for("run", "unknown")


def test_checked_in_schema_is_available() -> None:
    schema = outcome_event_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["event_type"]["const"] == EVENT_TYPE
    assert "reconciliation" in schema["properties"]["stage"]["enum"]
    assert "cache_read_tokens" in schema["$defs"]["metrics"]["properties"]
    assert "cache_write_tokens" in schema["$defs"]["metrics"]["properties"]
