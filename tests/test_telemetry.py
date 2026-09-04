"""Tests for generic event validation and OTLP transport."""

import json
from dataclasses import dataclass
from unittest import mock

import pytest
from requests import RequestException

from agentic_ci.telemetry import (
    TelemetryEventError,
    TelemetryExportError,
    append_event,
    build_event_record,
    emit_event,
    export_event,
    validate_event,
)


@dataclass(frozen=True)
class EventObject:
    event_type: str
    event_id: str

    def to_dict(self):
        return {"event_type": self.event_type, "event_id": self.event_id, "value": {"ok": True}}


def test_validate_accepts_mapping_and_protocol():
    mapping = {"event_type": "workflow.completed", "event_id": "event-1", "value": 3}

    assert validate_event(mapping) == mapping
    assert validate_event(EventObject("workflow.completed", "event-2"))["event_id"] == "event-2"


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"event_type": ""},
        {"event_type": "workflow.completed", "value": float("inf")},
        {"event_type": "workflow.completed", 1: "invalid"},
        object(),
    ],
)
def test_validate_rejects_invalid_events(event):
    with pytest.raises(TelemetryEventError):
        validate_event(event)


def test_build_record_keeps_event_schema_and_correlation_generic():
    record = build_event_record(
        EventObject("workflow.completed", "event-3"),
        correlation={
            "work_item": "item-3",
            "pipeline": "pipeline-4",
            "trace_id": "a" * 32,
        },
    )
    span = record["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attributes = {item["key"]: item["value"]["stringValue"] for item in span["attributes"]}

    assert record["path"] == "/v1/traces"
    assert span["traceId"] == "a" * 32
    assert span["status"]["code"] == 0
    assert attributes["telemetry.correlation.work_item"] == "item-3"
    assert attributes["telemetry.correlation.pipeline"] == "pipeline-4"
    assert json.loads(span["events"][0]["attributes"][0]["value"]["stringValue"]) == {
        "event_type": "workflow.completed",
        "event_id": "event-3",
        "value": {"ok": True},
    }


def test_event_type_only_records_get_unique_trace_ids():
    first = build_event_record({"event_type": "workflow.completed"})
    second = build_event_record({"event_type": "workflow.completed"})

    first_span = first["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    second_span = second["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert first_span["traceId"] != second_span["traceId"]


def test_append_event_joins_existing_trace_root(tmp_path):
    log_root = tmp_path / "run"
    log_file = log_root / "claude-otel.jsonl"
    root = {
        "path": "/v1/traces",
        "payload": {
            "resourceSpans": [
                {"scopeSpans": [{"spans": [{"traceId": "b" * 32, "spanId": "c" * 16}]}]}
            ]
        },
    }
    log_root.mkdir()
    log_file.write_text(json.dumps(root) + "\n", encoding="utf-8")

    record = append_event(
        log_root,
        {"event_type": "workflow.completed", "event_id": "event-4"},
        correlation={"trace_id": "b" * 32},
    )
    span = record["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

    assert span["parentSpanId"] == "c" * 16
    assert len(log_file.read_text(encoding="utf-8").splitlines()) == 2


def test_emit_event_requires_destination():
    with pytest.raises(TelemetryEventError, match="log_root or endpoint"):
        emit_event({"event_type": "workflow.completed"})


@pytest.mark.parametrize(
    "event",
    [
        {"event_type": "workflow.completed", "password": "not-safe"},
        {"event_type": "workflow.completed", "metadata": {"api-key": "not-safe"}},
        {"event_type": "workflow.completed", "metadata": {"provider_secret": "not-safe"}},
        {"event_type": "workflow.completed", "prompt": "summarize this"},
        {"event_type": "workflow.completed", "value": "Bearer abcdefghijklmnop"},
        {"event_type": "workflow.completed", "value": "owner@example.com"},
        {"event_type": "workflow.completed", "value": "https://user:pass@example.test/x"},
        {
            "event_type": "workflow.completed",
            "value": "https://collector.test/x?access_token=secret-value",
        },
    ],
)
def test_validate_rejects_sensitive_event_content(event):
    with pytest.raises(TelemetryEventError):
        validate_event(event)


def test_validate_allows_aggregate_tokens_and_safe_urls():
    event = {
        "event_type": "workflow.completed",
        "input_tokens": 10,
        "output_tokens": 4,
        "repository": "https://github.com/example/repository",
    }

    assert validate_event(event) == event


def test_build_record_rejects_sensitive_correlation():
    with pytest.raises(TelemetryEventError, match="sensitive content"):
        build_event_record(
            {"event_type": "workflow.completed"},
            correlation={"work_item": "owner@example.com"},
        )


def test_validate_rejects_oversized_events():
    event = {"event_type": "workflow.completed", "values": ["x" * 100] * 1_000}

    with pytest.raises(TelemetryEventError, match="maximum serialized size"):
        validate_event(event)


def test_append_event_rejects_symlink_log_file(tmp_path):
    log_root = tmp_path / "run"
    log_root.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("unchanged\n", encoding="utf-8")
    (log_root / "claude-otel.jsonl").symlink_to(outside)

    with pytest.raises(TelemetryEventError, match="regular file"):
        append_event(log_root, {"event_type": "workflow.completed"})

    assert outside.read_text(encoding="utf-8") == "unchanged\n"


def test_append_event_rejects_symlink_log_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    log_root = tmp_path / "run"
    log_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(TelemetryEventError, match="symbolic link"):
        append_event(log_root, {"event_type": "workflow.completed"})


def test_export_event_posts_otlp_payload():
    response = mock.Mock()
    with mock.patch("agentic_ci.telemetry.requests.post", return_value=response) as post:
        record = export_event(
            "http://collector:4318",
            {"event_type": "workflow.completed", "event_id": "event-5"},
            headers={"Authorization": "Bearer test"},
        )

    post.assert_called_once()
    assert post.call_args.args[0] == "http://collector:4318/v1/traces"
    assert post.call_args.kwargs["json"] == record["payload"]
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer test"}


def test_export_failure_is_reported_without_swallowing():
    with mock.patch(
        "agentic_ci.telemetry.requests.post",
        side_effect=RequestException("offline"),
    ):
        with pytest.raises(TelemetryExportError, match="offline"):
            export_event("http://collector:4318", {"event_type": "workflow.completed"})
