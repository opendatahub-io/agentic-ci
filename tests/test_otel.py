"""Tests for the OTLP HTTP/JSON collector."""

import json
import urllib.request

import pytest

from agentic_ci.otel import (
    _build_root_span_record,
    _find_orphan_traces,
    _has_any_traces,
    generate_trace_context,
    inject_root_spans,
    start_collector,
    stop_collector,
)


@pytest.fixture()
def collector(tmp_path):
    proc, port, log, _rate = start_collector(str(tmp_path))
    yield port, log
    stop_collector(proc)


def _post(port, path, body, chunked=False):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if chunked:
        req.remove_header("Content-length")
        req.add_header("Transfer-Encoding", "chunked")
    with urllib.request.urlopen(req) as resp:
        return resp.status


def _read_log(log_path):
    records = []
    with open(log_path) as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


class TestOTLPCollector:
    def test_content_length_metrics(self, collector):
        port, log = collector
        payload = {
            "resourceMetrics": [
                {
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "asInt": 500,
                                                "attributes": [
                                                    {
                                                        "key": "model",
                                                        "value": {"stringValue": "test-model"},
                                                    },
                                                    {
                                                        "key": "type",
                                                        "value": {"stringValue": "input"},
                                                    },
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        status = _post(port, "/v1/metrics", payload)
        assert status == 200

        records = _read_log(log)
        assert len(records) == 1
        assert records[0]["path"] == "/v1/metrics"
        assert "resourceMetrics" in records[0]["payload"]

    def test_content_length_logs(self, collector):
        port, log = collector
        payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": []}]}]}
        status = _post(port, "/v1/logs", payload)
        assert status == 200

        records = _read_log(log)
        assert len(records) == 1
        assert "resourceLogs" in records[0]["payload"]

    def test_chunked_metrics(self, collector):
        port, log = collector
        payload = {
            "resourceMetrics": [
                {
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {"dataPoints": [{"asInt": 1000}]},
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        status = _post(port, "/v1/metrics", payload, chunked=True)
        assert status == 200

        records = _read_log(log)
        assert len(records) == 1
        assert records[0]["path"] == "/v1/metrics"
        assert "resourceMetrics" in records[0]["payload"]

    def test_chunked_logs(self, collector):
        port, log = collector
        payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": []}]}]}
        status = _post(port, "/v1/logs", payload, chunked=True)
        assert status == 200

        records = _read_log(log)
        assert len(records) == 1
        assert "resourceLogs" in records[0]["payload"]

    def test_chunked_traces(self, collector):
        port, log = collector
        payload = {"resourceSpans": [{"scopeSpans": []}]}
        status = _post(port, "/v1/traces", payload, chunked=True)
        assert status == 200

        records = _read_log(log)
        assert len(records) == 1
        assert "resourceSpans" in records[0]["payload"]

    def test_empty_body(self, collector):
        port, log = collector
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/metrics",
            data=b"",
            headers={"Content-Type": "application/json", "Content-Length": "0"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200

        records = _read_log(log)
        assert len(records) == 1
        assert records[0]["payload"] == {}

    def test_multiple_requests_mixed_encoding(self, collector):
        port, log = collector
        _post(port, "/v1/metrics", {"resourceMetrics": [{"mode": "content-length"}]})
        _post(port, "/v1/logs", {"resourceLogs": [{"mode": "chunked"}]}, chunked=True)
        _post(port, "/v1/metrics", {"resourceMetrics": [{"mode": "content-length-2"}]})

        records = _read_log(log)
        assert len(records) == 3
        assert records[0]["payload"]["resourceMetrics"][0]["mode"] == "content-length"
        assert records[1]["payload"]["resourceLogs"][0]["mode"] == "chunked"
        assert records[2]["payload"]["resourceMetrics"][0]["mode"] == "content-length-2"


def _make_span(trace_id, span_id, parent_span_id="", name="test-span"):
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return span


def _make_trace_record(*spans):
    return {
        "ts": "2024-01-01T00:00:00+00:00",
        "path": "/v1/traces",
        "payload": {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]},
    }


class TestGenerateTraceContext:
    def test_format(self):
        trace_id, span_id, traceparent = generate_trace_context()
        assert len(trace_id) == 32
        assert len(span_id) == 16
        assert traceparent == f"00-{trace_id}-{span_id}-01"

    def test_uniqueness(self):
        ctx1 = generate_trace_context()
        ctx2 = generate_trace_context()
        assert ctx1[0] != ctx2[0]
        assert ctx1[1] != ctx2[1]

    def test_hex_chars(self):
        trace_id, span_id, _ = generate_trace_context()
        int(trace_id, 16)
        int(span_id, 16)


class TestFindOrphanTraces:
    def test_no_records(self):
        assert _find_orphan_traces([]) == {}

    def test_complete_trace(self):
        root = _make_span("aaaa" * 8, "bbbb" * 4)
        child = _make_span("aaaa" * 8, "cccc" * 4, parent_span_id="bbbb" * 4)
        records = [_make_trace_record(root, child)]
        assert _find_orphan_traces(records) == {}

    def test_orphan_trace(self):
        child = _make_span("aaaa" * 8, "cccc" * 4, parent_span_id="bbbb" * 4)
        records = [_make_trace_record(child)]
        orphans = _find_orphan_traces(records)
        assert "aaaa" * 8 in orphans

    def test_mixed_complete_and_orphan(self):
        root = _make_span("aaaa" * 8, "1111" * 4)
        child1 = _make_span("aaaa" * 8, "2222" * 4, parent_span_id="1111" * 4)
        orphan_child = _make_span("bbbb" * 8, "3333" * 4, parent_span_id="4444" * 4)
        records = [_make_trace_record(root, child1), _make_trace_record(orphan_child)]
        orphans = _find_orphan_traces(records)
        assert "aaaa" * 8 not in orphans
        assert "bbbb" * 8 in orphans

    def test_ignores_non_trace_records(self):
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "path": "/v1/metrics", "payload": {}},
        ]
        assert _find_orphan_traces(records) == {}


class TestHasAnyTraces:
    def test_no_traces(self):
        assert _has_any_traces([]) is False

    def test_with_spans(self):
        span = _make_span("aaaa" * 8, "bbbb" * 4)
        records = [_make_trace_record(span)]
        assert _has_any_traces(records) is True

    def test_empty_spans(self):
        records = [
            {
                "ts": "2024-01-01T00:00:00+00:00",
                "path": "/v1/traces",
                "payload": {"resourceSpans": [{"scopeSpans": [{"spans": []}]}]},
            }
        ]
        assert _has_any_traces(records) is False


class TestBuildRootSpanRecord:
    def test_success_status(self):
        record = _build_root_span_record("aa" * 16, "bb" * 8, 1000, 2000, exit_code=0)
        spans = record["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        span = spans[0]
        assert span["traceId"] == "aa" * 16
        assert span["spanId"] == "bb" * 8
        assert span["name"] == "agentic-ci-run"
        assert span["status"]["code"] == 1
        assert "message" not in span["status"]

    def test_error_status(self):
        record = _build_root_span_record("aa" * 16, "bb" * 8, 1000, 2000, exit_code=137)
        span = record["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["status"]["code"] == 2
        assert "137" in span["status"]["message"]

    def test_attributes(self):
        record = _build_root_span_record(
            "aa" * 16,
            "bb" * 8,
            1000,
            2000,
            exit_code=0,
            attributes={"agent.backend": "podman"},
        )
        span = record["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attr_keys = [a["key"] for a in span["attributes"]]
        assert "agentic_ci.synthetic_root" in attr_keys
        assert "agent.backend" in attr_keys

    def test_synthetic_marker(self):
        record = _build_root_span_record("aa" * 16, "bb" * 8, 1000, 2000, exit_code=0)
        span = record["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        synthetic = next(a for a in span["attributes"] if a["key"] == "agentic_ci.synthetic_root")
        assert synthetic["value"]["boolValue"] is True


class TestInjectRootSpans:
    def test_injects_for_orphan_trace(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        child = _make_span("aaaa" * 8, "cccc" * 4, parent_span_id="bbbb" * 4)
        record = _make_trace_record(child)
        with open(log_file, "w") as f:
            f.write(json.dumps(record) + "\n")

        injected = inject_root_spans(log_file, 1000, 2000, exit_code=1)
        assert injected == 1

        records = _read_log(log_file)
        assert len(records) == 2
        new_span = records[1]["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert new_span["traceId"] == "aaaa" * 8
        assert new_span["status"]["code"] == 2

    def test_no_injection_for_complete_trace(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        root = _make_span("aaaa" * 8, "bbbb" * 4)
        record = _make_trace_record(root)
        with open(log_file, "w") as f:
            f.write(json.dumps(record) + "\n")

        injected = inject_root_spans(log_file, 1000, 2000, exit_code=0)
        assert injected == 0

        records = _read_log(log_file)
        assert len(records) == 1

    def test_fallback_trace_on_empty_log(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        with open(log_file, "w") as f:
            f.write("")

        injected = inject_root_spans(
            log_file, 1000, 2000, exit_code=137, fallback_trace_id="ffff" * 8
        )
        assert injected == 1

        records = _read_log(log_file)
        assert len(records) == 1
        span = records[0]["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["traceId"] == "ffff" * 8
        assert span["status"]["code"] == 2

    def test_no_fallback_when_traces_exist(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        root = _make_span("aaaa" * 8, "bbbb" * 4)
        record = _make_trace_record(root)
        with open(log_file, "w") as f:
            f.write(json.dumps(record) + "\n")

        injected = inject_root_spans(
            log_file, 1000, 2000, exit_code=0, fallback_trace_id="ffff" * 8
        )
        assert injected == 0

    def test_missing_log_file_with_fallback(self, tmp_path):
        log_file = str(tmp_path / "nonexistent.jsonl")
        injected = inject_root_spans(
            log_file, 1000, 2000, exit_code=1, fallback_trace_id="ffff" * 8
        )
        assert injected == 1

    def test_attributes_passed_through(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        child = _make_span("aaaa" * 8, "cccc" * 4, parent_span_id="bbbb" * 4)
        record = _make_trace_record(child)
        with open(log_file, "w") as f:
            f.write(json.dumps(record) + "\n")

        inject_root_spans(
            log_file,
            1000,
            2000,
            exit_code=0,
            attributes={"agent.model": "test-model"},
        )

        records = _read_log(log_file)
        span = records[1]["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attr_keys = [a["key"] for a in span["attributes"]]
        assert "agent.model" in attr_keys

    def test_multiple_orphan_traces(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        child1 = _make_span("aaaa" * 8, "1111" * 4, parent_span_id="0000" * 4)
        child2 = _make_span("bbbb" * 8, "2222" * 4, parent_span_id="0000" * 4)
        with open(log_file, "w") as f:
            f.write(json.dumps(_make_trace_record(child1)) + "\n")
            f.write(json.dumps(_make_trace_record(child2)) + "\n")

        injected = inject_root_spans(log_file, 1000, 2000, exit_code=1)
        assert injected == 2

    def test_malformed_json_line_skipped(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        child = _make_span("aaaa" * 8, "cccc" * 4, parent_span_id="bbbb" * 4)
        record = _make_trace_record(child)
        with open(log_file, "w") as f:
            f.write(json.dumps(record) + "\n")
            f.write('{"truncated": true\n')

        injected = inject_root_spans(log_file, 1000, 2000, exit_code=1)
        assert injected == 1

        with open(log_file) as f:
            valid = []
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    valid.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
        assert len(valid) == 2
        assert (
            valid[1]["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]
            == "aaaa" * 8
        )

    def test_fallback_span_id_reused(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        with open(log_file, "w") as f:
            f.write("")

        injected = inject_root_spans(
            log_file,
            1000,
            2000,
            exit_code=0,
            fallback_trace_id="aaaa" * 8,
            fallback_span_id="bbbb" * 4,
        )
        assert injected == 1

        records = _read_log(log_file)
        span = records[0]["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["spanId"] == "bbbb" * 4

    def test_timing_uses_orchestrator_bounds(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        child = _make_span("aaaa" * 8, "cccc" * 4, parent_span_id="bbbb" * 4)
        child["startTimeUnixNano"] = "5000"
        child["endTimeUnixNano"] = "6000"
        record = _make_trace_record(child)
        with open(log_file, "w") as f:
            f.write(json.dumps(record) + "\n")

        inject_root_spans(log_file, 1000, 9000, exit_code=0)

        records = _read_log(log_file)
        span = records[1]["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert int(span["startTimeUnixNano"]) == 1000
        assert int(span["endTimeUnixNano"]) == 9000
