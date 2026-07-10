"""Tests for the OTLP HTTP/JSON collector."""

import json
import urllib.request

import pytest

from agentic_ci.otel import start_collector, stop_collector, wait_for_otel_complete


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


def _get_status(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/otel-status", timeout=2) as resp:
        return json.loads(resp.read())


def _trace_payload(spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


class TestRootSpanTracking:
    def test_status_empty_before_traces(self, collector):
        port, _log = collector
        data = _get_status(port)
        assert data["traces"] == {}
        assert data["all_complete"] is False

    def test_tracks_root_span(self, collector):
        port, _log = collector
        _post(
            port,
            "/v1/traces",
            _trace_payload([{"traceId": "aabb", "spanId": "0001", "name": "root"}]),
        )
        data = _get_status(port)
        assert data["traces"]["aabb"] is True
        assert data["all_complete"] is True

    def test_tracks_orphan_span(self, collector):
        port, _log = collector
        _post(
            port,
            "/v1/traces",
            _trace_payload(
                [{"traceId": "ccdd", "spanId": "0002", "parentSpanId": "missing", "name": "child"}]
            ),
        )
        data = _get_status(port)
        assert data["traces"]["ccdd"] is False
        assert data["all_complete"] is False

    def test_root_completes_after_orphan(self, collector):
        port, _log = collector
        _post(
            port,
            "/v1/traces",
            _trace_payload(
                [{"traceId": "eeff", "spanId": "0003", "parentSpanId": "0004", "name": "child"}]
            ),
        )
        assert _get_status(port)["traces"]["eeff"] is False

        _post(
            port,
            "/v1/traces",
            _trace_payload([{"traceId": "eeff", "spanId": "0004", "name": "root"}]),
        )
        assert _get_status(port)["traces"]["eeff"] is True
        assert _get_status(port)["all_complete"] is True

    def test_mixed_traces(self, collector):
        port, _log = collector
        _post(
            port,
            "/v1/traces",
            _trace_payload([{"traceId": "t1", "spanId": "s1", "name": "root"}]),
        )
        _post(
            port,
            "/v1/traces",
            _trace_payload(
                [{"traceId": "t2", "spanId": "s2", "parentSpanId": "missing", "name": "orphan"}]
            ),
        )
        data = _get_status(port)
        assert data["traces"]["t1"] is True
        assert data["traces"]["t2"] is False
        assert data["all_complete"] is False


class TestWaitForOtelComplete:
    def test_returns_true_when_complete(self, collector):
        port, _log = collector
        _post(
            port,
            "/v1/traces",
            _trace_payload([{"traceId": "t1", "spanId": "s1", "name": "root"}]),
        )
        assert wait_for_otel_complete(port, timeout=2) is True

    def test_returns_false_on_timeout(self, collector):
        port, _log = collector
        _post(
            port,
            "/v1/traces",
            _trace_payload(
                [{"traceId": "t1", "spanId": "s1", "parentSpanId": "missing", "name": "orphan"}]
            ),
        )
        assert wait_for_otel_complete(port, timeout=1, poll_interval=0.2) is False

    def test_returns_false_when_proc_dead(self, collector):
        port, _log = collector
        _post(
            port,
            "/v1/traces",
            _trace_payload(
                [{"traceId": "t1", "spanId": "s1", "parentSpanId": "missing", "name": "orphan"}]
            ),
        )
        import subprocess

        dead_proc = subprocess.Popen(["true"])
        dead_proc.wait()
        assert wait_for_otel_complete(port, agent_proc=dead_proc, timeout=2) is False
