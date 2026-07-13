"""E2E tests for mlflow-push against a real MLflow server.

Starts a local MLflow server, generates synthetic OTEL JSONL data, pushes
it through push_traces(), and verifies the results via the MLflow Python
client.  Catches API contract mismatches (field names, endpoint versions)
that unit tests with mocked HTTP cannot detect.

Run with: tox -e mlflow-e2e
"""

import json
import signal
import subprocess
import sys
import time

import mlflow
import pytest

from agentic_ci.mlflow import push_traces
from agentic_ci.otel import inject_root_spans

_EXPERIMENT = "mlflow-e2e-test"


def _wait_for_server(port, timeout=15):
    """Poll until the MLflow server responds."""
    import requests

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(
                f"http://127.0.0.1:{port}/api/2.0/mlflow/experiments/search",
                json={"filter": "", "max_results": 1},
                timeout=2,
            )
            if resp.status_code == 200:
                return
        except requests.ConnectionError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"MLflow server on port {port} did not start within {timeout}s")


@pytest.fixture(scope="module")
def mlflow_server(tmp_path_factory):
    """Start a local MLflow server for the test session."""
    tmp = tmp_path_factory.mktemp("mlflow")
    db_path = tmp / "mlflow.db"
    artifacts = tmp / "artifacts"
    artifacts.mkdir()

    port = 15555
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mlflow",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--backend-store-uri",
            f"sqlite:///{db_path}",
            "--artifacts-destination",
            str(artifacts),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_server(port)
        endpoint = f"http://127.0.0.1:{port}"
        mlflow.set_tracking_uri(endpoint)
        mlflow.create_experiment(_EXPERIMENT)
        yield endpoint
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture()
def client():
    return mlflow.MlflowClient()


def _write_jsonl(tmp_path, records):
    path = tmp_path / "claude-otel.jsonl"
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return str(path)


def _trace_record(spans, trace_id="aabb112233445566aabb112233445566"):
    """Build an OTLP trace JSONL record."""
    for s in spans:
        s.setdefault("traceId", trace_id)
        s.setdefault("kind", 1)
        s.setdefault("startTimeUnixNano", "1000000000")
        s.setdefault("endTimeUnixNano", "2000000000")
        s.setdefault("status", {"code": 0})
        s.setdefault("attributes", [])
    return {
        "path": "/v1/traces",
        "payload": {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]},
    }


def _metric_record(session_id, cost):
    """Build an OTLP metrics JSONL record with a cost data point."""
    return {
        "path": "/v1/metrics",
        "payload": {
            "resourceMetrics": [
                {
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.cost.usage",
                                    "sum": {
                                        "aggregationTemporality": 1,
                                        "dataPoints": [
                                            {
                                                "asDouble": cost,
                                                "attributes": [
                                                    {
                                                        "key": "session.id",
                                                        "value": {
                                                            "stringValue": session_id,
                                                        },
                                                    }
                                                ],
                                            }
                                        ],
                                    },
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    }


def _log_record(request_id, query_source):
    """Build an OTLP logs JSONL record with a query_source event."""
    return {
        "path": "/v1/logs",
        "payload": {
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "attributes": [
                                        {
                                            "key": "event.name",
                                            "value": {
                                                "stringValue": "claude_code.api_request",
                                            },
                                        },
                                        {
                                            "key": "request_id",
                                            "value": {"stringValue": request_id},
                                        },
                                        {
                                            "key": "query_source",
                                            "value": {"stringValue": query_source},
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    }


class TestTraceWithRoot:
    """Trace that has a root span should land as OK."""

    def test_ok_trace(self, mlflow_server, client, tmp_path):
        tid = "00000000000000010000000000000001"
        root = {
            "spanId": "0000000000000001",
            "name": "claude_code.session",
            "traceId": tid,
        }
        child = {
            "spanId": "0000000000000002",
            "parentSpanId": "0000000000000001",
            "name": "claude_code.llm_request",
            "traceId": tid,
            "attributes": [
                {"key": "input_tokens", "value": {"intValue": "100"}},
                {"key": "output_tokens", "value": {"intValue": "50"}},
            ],
        }
        path = _write_jsonl(tmp_path, [_trace_record([root, child], trace_id=tid)])
        result = push_traces(path, mlflow_server, _EXPERIMENT)

        assert result.ok == 1
        assert result.err == 0
        assert f"tr-{tid}" in result.trace_ids

        trace = client.get_trace(f"tr-{tid}")
        assert str(trace.info.status) == "TraceStatus.OK"
        assert len(trace.data.spans) == 2


class TestTraceWithoutRoot:
    """Trace with missing root span should be finalized to ERROR."""

    def test_finalized_to_error(self, mlflow_server, client, tmp_path):
        tid = "00000000000000020000000000000002"
        child1 = {
            "spanId": "0000000000000003",
            "parentSpanId": "aaaa000000000000",
            "name": "claude_code.llm_request",
            "traceId": tid,
        }
        child2 = {
            "spanId": "0000000000000004",
            "parentSpanId": "aaaa000000000000",
            "name": "claude_code.tool",
            "traceId": tid,
        }
        path = _write_jsonl(tmp_path, [_trace_record([child1, child2], trace_id=tid)])
        result = push_traces(path, mlflow_server, _EXPERIMENT)

        assert result.ok == 1
        assert result.err == 0

        trace = client.get_trace(f"tr-{tid}")
        assert str(trace.info.status) == "TraceStatus.ERROR"


class TestTokenUsage:
    """Token usage attributes should be mapped to MLflow conventions."""

    def test_token_and_genai_attrs(self, mlflow_server, client, tmp_path):
        tid = "00000000000000030000000000000003"
        root = {
            "spanId": "0000000000000005",
            "name": "claude_code.session",
            "traceId": tid,
        }
        llm = {
            "spanId": "0000000000000006",
            "parentSpanId": "0000000000000005",
            "name": "claude_code.llm_request",
            "traceId": tid,
            "attributes": [
                {"key": "input_tokens", "value": {"intValue": "10"}},
                {"key": "output_tokens", "value": {"intValue": "200"}},
                {"key": "cache_read_tokens", "value": {"intValue": "5000"}},
                {"key": "cache_creation_tokens", "value": {"intValue": "1000"}},
            ],
        }
        path = _write_jsonl(tmp_path, [_trace_record([root, llm], trace_id=tid)])
        push_traces(path, mlflow_server, _EXPERIMENT)

        trace = client.get_trace(f"tr-{tid}")
        llm_span = next(s for s in trace.data.spans if s.name == "claude_code.llm_request")

        usage = llm_span.attributes.get("mlflow.chat.tokenUsage")
        assert usage is not None
        if isinstance(usage, str):
            usage = json.loads(usage)
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 200
        assert usage["total_tokens"] == 210
        assert usage["cache_read_input_tokens"] == 5000
        assert usage["cache_creation_input_tokens"] == 1000

        assert int(llm_span.attributes["gen_ai.usage.input_tokens"]) == 10
        assert int(llm_span.attributes["gen_ai.usage.output_tokens"]) == 200


class TestCostAttribution:
    """Cost from /v1/metrics should be distributed across spans."""

    def test_cost_distributed(self, mlflow_server, client, tmp_path):
        tid = "00000000000000040000000000000004"
        sid = "cost-test-session-1"
        root = {
            "spanId": "0000000000000007",
            "name": "claude_code.session",
            "traceId": tid,
        }
        llm1 = {
            "spanId": "0000000000000008",
            "parentSpanId": "0000000000000007",
            "name": "claude_code.llm_request",
            "traceId": tid,
            "attributes": [
                {"key": "session.id", "value": {"stringValue": sid}},
                {"key": "input_tokens", "value": {"intValue": "100"}},
                {"key": "output_tokens", "value": {"intValue": "0"}},
            ],
        }
        llm2 = {
            "spanId": "0000000000000009",
            "parentSpanId": "0000000000000007",
            "name": "claude_code.llm_request",
            "traceId": tid,
            "attributes": [
                {"key": "session.id", "value": {"stringValue": sid}},
                {"key": "input_tokens", "value": {"intValue": "0"}},
                {"key": "output_tokens", "value": {"intValue": "300"}},
            ],
        }
        records = [
            _trace_record([root, llm1, llm2], trace_id=tid),
            _metric_record(sid, 0.40),
        ]
        path = _write_jsonl(tmp_path, records)
        push_traces(path, mlflow_server, _EXPERIMENT)

        trace = client.get_trace(f"tr-{tid}")
        llm_spans = [s for s in trace.data.spans if s.name == "claude_code.llm_request"]
        costs = []
        for s in llm_spans:
            raw = s.attributes.get("mlflow.llm.cost")
            assert raw is not None, f"span {s.span_id} missing mlflow.llm.cost"
            cost_data = json.loads(raw) if isinstance(raw, str) else raw
            costs.append(cost_data["total_cost"])

        assert abs(sum(costs) - 0.40) < 1e-9


class TestQuerySource:
    """query_source from /v1/logs should be joined to spans by request_id."""

    def test_source_tagged(self, mlflow_server, client, tmp_path):
        tid = "00000000000000050000000000000005"
        root = {
            "spanId": "000000000000000a",
            "name": "claude_code.session",
            "traceId": tid,
        }
        llm = {
            "spanId": "000000000000000b",
            "parentSpanId": "000000000000000a",
            "name": "claude_code.llm_request",
            "traceId": tid,
            "attributes": [
                {"key": "request_id", "value": {"stringValue": "req-42"}},
            ],
        }
        records = [
            _trace_record([root, llm], trace_id=tid),
            _log_record("req-42", "generate_session_title"),
        ]
        path = _write_jsonl(tmp_path, records)
        push_traces(path, mlflow_server, _EXPERIMENT)

        trace = client.get_trace(f"tr-{tid}")
        llm_span = next(s for s in trace.data.spans if s.name == "claude_code.llm_request")
        assert llm_span.attributes.get("query_source") == "generate_session_title"


class TestMultiPayloadTrace:
    """Spans for the same trace split across multiple OTLP payloads
    should be merged into one trace by MLflow."""

    def test_merged(self, mlflow_server, client, tmp_path):
        tid = "00000000000000060000000000000006"
        root = {
            "spanId": "000000000000000c",
            "name": "claude_code.session",
            "traceId": tid,
        }
        child1 = {
            "spanId": "000000000000000d",
            "parentSpanId": "000000000000000c",
            "name": "claude_code.llm_request",
            "traceId": tid,
        }
        child2 = {
            "spanId": "000000000000000e",
            "parentSpanId": "000000000000000c",
            "name": "claude_code.tool",
            "traceId": tid,
            "startTimeUnixNano": "3000000000",
            "endTimeUnixNano": "4000000000",
        }
        records = [
            _trace_record([root, child1], trace_id=tid),
            _trace_record([child2], trace_id=tid),
        ]
        path = _write_jsonl(tmp_path, records)
        result = push_traces(path, mlflow_server, _EXPERIMENT)

        assert result.ok == 2

        trace = client.get_trace(f"tr-{tid}")
        assert str(trace.info.status) == "TraceStatus.OK"
        assert len(trace.data.spans) == 3


class TestOTLPJsonAccepted:
    """Verify payloads are sent as JSON (not protobuf)."""

    def test_hex_ids_preserved(self, mlflow_server, client, tmp_path):
        tid = "00000000000000070000000000000007"
        span = {
            "spanId": "000000000000000f",
            "name": "claude_code.session",
            "traceId": tid,
        }
        path = _write_jsonl(tmp_path, [_trace_record([span], trace_id=tid)])
        result = push_traces(path, mlflow_server, _EXPERIMENT)

        assert result.ok == 1
        trace = client.get_trace(f"tr-{tid}")
        assert trace is not None
        assert len(trace.data.spans) == 1


class TestSyntheticRootSpan:
    """Orchestrator-injected synthetic root spans should produce
    complete traces in MLflow without relying on _finalize_traces."""

    def test_synthetic_root_makes_trace_ok(self, mlflow_server, client, tmp_path):
        """Orphan spans + inject_root_spans -> push -> trace is OK."""
        tid = "00000000000000080000000000000008"
        child = {
            "spanId": "0000000000000010",
            "parentSpanId": "aaaa000000000001",
            "name": "claude_code.llm_request",
            "traceId": tid,
        }
        path = _write_jsonl(tmp_path, [_trace_record([child], trace_id=tid)])

        injected = inject_root_spans(
            path,
            start_ns=500_000_000,
            end_ns=3_000_000_000,
            exit_code=0,
            attributes={"agent.backend": "podman"},
        )
        assert injected == 1

        result = push_traces(path, mlflow_server, _EXPERIMENT)
        assert result.ok == 2
        assert result.err == 0

        trace = client.get_trace(f"tr-{tid}")
        assert str(trace.info.status) == "TraceStatus.OK"
        assert len(trace.data.spans) == 2

        root_spans = [s for s in trace.data.spans if s.name == "agentic-ci-run"]
        assert len(root_spans) == 1
        assert root_spans[0].attributes.get("agentic_ci.synthetic_root") is True

    def test_synthetic_root_with_error_exit(self, mlflow_server, client, tmp_path):
        """Container crash (exit 137) produces an ERROR trace."""
        tid = "00000000000000090000000000000009"
        child = {
            "spanId": "0000000000000011",
            "parentSpanId": "aaaa000000000002",
            "name": "claude_code.tool",
            "traceId": tid,
        }
        path = _write_jsonl(tmp_path, [_trace_record([child], trace_id=tid)])

        inject_root_spans(path, 500_000_000, 3_000_000_000, exit_code=137)

        result = push_traces(path, mlflow_server, _EXPERIMENT)
        assert result.ok == 2

        trace = client.get_trace(f"tr-{tid}")
        assert str(trace.info.status) == "TraceStatus.ERROR"

    def test_fallback_root_on_zero_spans(self, mlflow_server, client, tmp_path):
        """Total crash (no spans at all) still produces a trace via fallback."""
        tid = "0000000000000a000000000000000a00"
        path = _write_jsonl(tmp_path, [])

        inject_root_spans(path, 1_000_000_000, 2_000_000_000, exit_code=1, fallback_trace_id=tid)

        result = push_traces(path, mlflow_server, _EXPERIMENT)
        assert result.ok == 1

        trace = client.get_trace(f"tr-{tid}")
        assert str(trace.info.status) == "TraceStatus.ERROR"
        assert len(trace.data.spans) == 1
        assert trace.data.spans[0].name == "agentic-ci-run"
