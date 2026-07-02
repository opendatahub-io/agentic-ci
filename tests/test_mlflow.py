"""Tests for agentic_ci.mlflow — protobuf serialization and trace push."""

import base64
import copy
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from agentic_ci.mlflow import (
    _add_query_source,
    _add_span_costs,
    _add_token_usage,
    _cost_by_session,
    _fixup_ids,
    _hex_to_base64,
    _query_source_by_request,
    _serialize_traces,
    push_traces,
)

# Hex-encoded IDs (what Claude Code / OTLP JSON exporters produce)
HEX_TRACE_PAYLOAD = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": [{"key": "service.name", "value": {"stringValue": "test-agent"}}]
            },
            "scopeSpans": [
                {
                    "scope": {"name": "test"},
                    "spans": [
                        {
                            "traceId": "0af7651916cd43dd8448eb211c80319c",
                            "spanId": "b7ad6b7169203331",
                            "parentSpanId": "00f067aa0ba902b7",
                            "name": "test-span",
                            "kind": 1,
                            "startTimeUnixNano": "1000000000",
                            "endTimeUnixNano": "2000000000",
                            "attributes": [
                                {
                                    "key": "test.key",
                                    "value": {"stringValue": "test-value"},
                                }
                            ],
                            "status": {},
                        }
                    ],
                }
            ],
        }
    ]
}


class TestHexToBase64:
    def test_converts_32char_trace_id(self):
        result = _hex_to_base64("0af7651916cd43dd8448eb211c80319c")
        assert result != "0af7651916cd43dd8448eb211c80319c"
        assert bytes.fromhex("0af7651916cd43dd8448eb211c80319c") == base64.b64decode(result)

    def test_converts_16char_span_id(self):
        result = _hex_to_base64("b7ad6b7169203331")
        assert result != "b7ad6b7169203331"

    def test_passes_through_base64(self):
        b64 = "CvdlGRbNQ92ESOshHIAxnA=="
        assert _hex_to_base64(b64) == b64

    def test_passes_through_non_hex(self):
        assert _hex_to_base64("not-hex-string") == "not-hex-string"
        assert _hex_to_base64("") == ""
        assert _hex_to_base64(123) == 123


class TestFixupIds:
    def test_converts_all_id_fields(self):
        payload = copy.deepcopy(HEX_TRACE_PAYLOAD)
        fixed = _fixup_ids(payload)
        span = fixed["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["traceId"] != "0af7651916cd43dd8448eb211c80319c"
        assert span["spanId"] != "b7ad6b7169203331"
        assert span["parentSpanId"] != "00f067aa0ba902b7"


class TestSerializeTraces:
    def test_round_trip_with_hex_ids(self):
        serialized = _serialize_traces(copy.deepcopy(HEX_TRACE_PAYLOAD))
        assert isinstance(serialized, bytes)
        assert len(serialized) > 0

        deserialized = ExportTraceServiceRequest()
        deserialized.ParseFromString(serialized)
        assert len(deserialized.resource_spans) == 1
        span = deserialized.resource_spans[0].scope_spans[0].spans[0]
        assert span.name == "test-span"
        assert span.trace_id == bytes.fromhex("0af7651916cd43dd8448eb211c80319c")
        assert span.span_id == bytes.fromhex("b7ad6b7169203331")

    def test_does_not_mutate_input(self):
        payload = copy.deepcopy(HEX_TRACE_PAYLOAD)
        original_id = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]
        _serialize_traces(payload)
        assert payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] == original_id

    def test_empty_payload(self):
        serialized = _serialize_traces({})
        assert isinstance(serialized, bytes)
        deserialized = ExportTraceServiceRequest()
        deserialized.ParseFromString(serialized)
        assert len(deserialized.resource_spans) == 0

    def test_preserves_attributes(self):
        serialized = _serialize_traces(copy.deepcopy(HEX_TRACE_PAYLOAD))
        deserialized = ExportTraceServiceRequest()
        deserialized.ParseFromString(serialized)
        resource_attrs = deserialized.resource_spans[0].resource.attributes
        assert any(a.key == "service.name" for a in resource_attrs)


def _llm_span(attrs):
    return {
        "resourceSpans": [
            {"scopeSpans": [{"spans": [{"name": "claude_code.llm_request", "attributes": attrs}]}]}
        ]
    }


def _span_attrs(payload):
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    return {a["key"]: a["value"] for a in span["attributes"]}


def _chat_usage(payload):
    raw = _span_attrs(payload).get("mlflow.chat.tokenUsage")
    return json.loads(raw["stringValue"]) if raw else None


def _genai(payload, key):
    raw = _span_attrs(payload).get(key)
    return int(raw["intValue"]) if raw else None


class TestAddTokenUsage:
    def test_disjoint_breakdown_with_cache(self):
        payload = _llm_span(
            [
                {"key": "input_tokens", "value": {"intValue": "3"}},
                {"key": "output_tokens", "value": {"intValue": "148"}},
                {"key": "cache_read_tokens", "value": {"intValue": "1000"}},
                {"key": "cache_creation_tokens", "value": {"intValue": "39565"}},
            ]
        )
        _add_token_usage(payload)
        # MLflow native usage: disjoint, with cache
        assert _chat_usage(payload) == {
            "input_tokens": 3,
            "output_tokens": 148,
            "total_tokens": 151,  # cache excluded
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 39565,
        }
        # OTEL GenAI standard: fresh input + output (no cache field)
        assert _genai(payload, "gen_ai.usage.input_tokens") == 3
        assert _genai(payload, "gen_ai.usage.output_tokens") == 148

    def test_omits_zero_cache_keys(self):
        payload = _llm_span(
            [
                {"key": "input_tokens", "value": {"intValue": "10"}},
                {"key": "output_tokens", "value": {"intValue": "20"}},
            ]
        )
        _add_token_usage(payload)
        usage = _chat_usage(payload)
        assert usage == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
        assert "cache_read_input_tokens" not in usage

    def test_sets_usage_for_cache_only_turn(self):
        # all cache read (fresh input 0) still counts
        payload = _llm_span(
            [
                {"key": "input_tokens", "value": {"intValue": "0"}},
                {"key": "output_tokens", "value": {"intValue": "271"}},
                {"key": "cache_read_tokens", "value": {"intValue": "39774"}},
            ]
        )
        _add_token_usage(payload)
        usage = _chat_usage(payload)
        assert usage["cache_read_input_tokens"] == 39774
        assert usage["total_tokens"] == 271

    def test_skips_when_all_zero(self):
        payload = _llm_span(
            [
                {"key": "input_tokens", "value": {"intValue": "0"}},
                {"key": "output_tokens", "value": {"intValue": "0"}},
            ]
        )
        _add_token_usage(payload)
        assert "mlflow.chat.tokenUsage" not in _span_attrs(payload)

    def test_does_not_override_existing(self):
        usage_json = '{"input_tokens": 999}'
        preset = {"key": "mlflow.chat.tokenUsage", "value": {"stringValue": usage_json}}
        genai_preset = {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "111"}}
        payload = _llm_span(
            [
                {"key": "input_tokens", "value": {"intValue": "5"}},
                {"key": "output_tokens", "value": {"intValue": "7"}},
                preset,
                genai_preset,
            ]
        )
        _add_token_usage(payload)
        assert _chat_usage(payload) == {"input_tokens": 999}
        assert _genai(payload, "gen_ai.usage.input_tokens") == 111

    def test_skips_negative_components(self):
        payload = _llm_span(
            [
                {"key": "input_tokens", "value": {"intValue": "-10"}},
                {"key": "output_tokens", "value": {"intValue": "20"}},
            ]
        )
        _add_token_usage(payload)
        assert _chat_usage(payload) is None
        assert _genai(payload, "gen_ai.usage.input_tokens") is None

    def test_serialize_traces_emits_chat_usage(self):
        payload = _llm_span(
            [
                {"key": "input_tokens", "value": {"intValue": "10"}},
                {"key": "output_tokens", "value": {"intValue": "20"}},
                {"key": "cache_read_tokens", "value": {"intValue": "5"}},
            ]
        )
        parsed = ExportTraceServiceRequest()
        parsed.ParseFromString(_serialize_traces(payload))
        span = parsed.resource_spans[0].scope_spans[0].spans[0]
        attrs = {a.key: a.value for a in span.attributes}
        assert "mlflow.chat.tokenUsage" in attrs
        usage = json.loads(attrs["mlflow.chat.tokenUsage"].string_value)
        assert usage["cache_read_input_tokens"] == 5
        assert usage["total_tokens"] == 30


class TestCostFromMetrics:
    def _metric_rec(self, points):
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
                                                    "asDouble": v,
                                                    "attributes": [
                                                        {
                                                            "key": "session.id",
                                                            "value": {"stringValue": sid},
                                                        }
                                                    ],
                                                }
                                                for sid, v in points
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

    def _span(self, sid, inp, out):
        return {
            "name": "claude_code.llm_request",
            "attributes": [
                {"key": "session.id", "value": {"stringValue": sid}},
                {"key": "input_tokens", "value": {"intValue": str(inp)}},
                {"key": "output_tokens", "value": {"intValue": str(out)}},
            ],
        }

    def _payload(self, spans):
        return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}

    def _costs(self, payload):
        out = []
        for sp in payload["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            c = next(
                (
                    a["value"]["stringValue"]
                    for a in sp["attributes"]
                    if a["key"] == "mlflow.llm.cost"
                ),
                None,
            )
            out.append(json.loads(c)["total_cost"] if c else None)
        return out

    def test_cost_by_session_sums_deltas(self):
        rec = self._metric_rec([("S1", 0.10), ("S1", 0.30), ("S2", 1.0)])
        result = _cost_by_session([rec])
        assert abs(result["S1"] - 0.40) < 1e-9
        assert abs(result["S2"] - 1.0) < 1e-9

    def test_cost_by_session_ignores_other_metrics(self):
        rec = {
            "path": "/v1/metrics",
            "payload": {
                "resourceMetrics": [
                    {"scopeMetrics": [{"metrics": [{"name": "x.other.metric", "sum": {}}]}]}
                ]
            },
        }
        assert _cost_by_session([rec]) == {}

    def test_distributes_cost_by_token_weight(self):
        payload = self._payload([self._span("S1", 100, 0), self._span("S1", 0, 300)])
        _add_span_costs([payload], {"S1": 0.40})
        costs = self._costs(payload)
        assert abs(costs[0] - 0.10) < 1e-9
        assert abs(costs[1] - 0.30) < 1e-9
        assert abs(sum(costs) - 0.40) < 1e-9

    def test_does_not_override_existing_cost(self):
        sp = self._span("S1", 100, 100)
        sp["attributes"].append(
            {"key": "mlflow.llm.cost", "value": {"stringValue": '{"total_cost": 9.9}'}}
        )
        payload = self._payload([sp])
        _add_span_costs([payload], {"S1": 0.5})
        assert self._costs(payload) == [9.9]

    def test_noop_without_metrics(self):
        payload = self._payload([self._span("S1", 1, 1)])
        _add_span_costs([payload], {})
        assert self._costs(payload) == [None]

    def test_skips_session_without_cost(self):
        payload = self._payload([self._span("S2", 10, 10)])
        _add_span_costs([payload], {"S1": 1.0})
        assert self._costs(payload) == [None]


def _log_rec(entries):
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
                                        {"key": "request_id", "value": {"stringValue": rid}},
                                        {"key": "query_source", "value": {"stringValue": qs}},
                                    ]
                                }
                                for rid, qs in entries
                            ]
                        }
                    ]
                }
            ]
        },
    }


class TestQuerySource:
    def test_maps_request_to_source(self):
        recs = [_log_rec([("req-1", "sdk"), ("req-2", "generate_session_title")])]
        assert _query_source_by_request(recs) == {
            "req-1": "sdk",
            "req-2": "generate_session_title",
        }

    def test_ignores_records_missing_fields(self):
        rec = {
            "path": "/v1/logs",
            "payload": {
                "resourceLogs": [
                    {
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "attributes": [
                                            {"key": "request_id", "value": {"stringValue": "req-x"}}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
        }
        assert _query_source_by_request([rec]) == {}

    def test_tags_span_by_request_id(self):
        payload = _llm_span([{"key": "request_id", "value": {"stringValue": "req-2"}}])
        _add_query_source([payload], {"req-2": "generate_session_title"})
        assert _span_attrs(payload)["query_source"]["stringValue"] == "generate_session_title"

    def test_skips_span_without_match(self):
        payload = _llm_span([{"key": "request_id", "value": {"stringValue": "req-unknown"}}])
        _add_query_source([payload], {"req-2": "sdk"})
        assert "query_source" not in _span_attrs(payload)

    def test_does_not_override_existing(self):
        payload = _llm_span(
            [
                {"key": "request_id", "value": {"stringValue": "req-2"}},
                {"key": "query_source", "value": {"stringValue": "keep"}},
            ]
        )
        _add_query_source([payload], {"req-2": "sdk"})
        assert _span_attrs(payload)["query_source"]["stringValue"] == "keep"

    def test_noop_without_logs(self):
        payload = _llm_span([{"key": "request_id", "value": {"stringValue": "req-2"}}])
        _add_query_source([payload], {})
        assert "query_source" not in _span_attrs(payload)


class TestPushTraces:
    def _write_jsonl(self, records):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        for rec in records:
            f.write(json.dumps(rec) + "\n")
        f.close()
        return f.name

    def test_missing_file_returns_zero(self):
        ok, err = push_traces("/nonexistent.jsonl", "http://mlflow:5000", "exp")
        assert (ok, err) == (0, 0)

    def test_no_trace_records_returns_zero(self):
        path = self._write_jsonl([{"path": "/v1/metrics", "payload": {}}])
        try:
            ok, err = push_traces(path, "http://mlflow:5000", "exp")
            assert (ok, err) == (0, 0)
        finally:
            os.unlink(path)

    @patch("agentic_ci.mlflow.requests.post")
    def test_sends_protobuf_content_type(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"experiments": [{"experiment_id": "123"}]}
        mock_post.return_value = mock_resp

        path = self._write_jsonl(
            [{"path": "/v1/traces", "payload": copy.deepcopy(HEX_TRACE_PAYLOAD)}]
        )
        try:
            ok, err = push_traces(path, "http://mlflow:5000", "exp")
        finally:
            os.unlink(path)

        assert ok == 1
        assert err == 0

        trace_call = mock_post.call_args_list[1]
        assert trace_call.kwargs["headers"]["Content-Type"] == "application/x-protobuf"
        assert trace_call.kwargs["headers"]["x-mlflow-experiment-id"] == "123"
        assert isinstance(trace_call.kwargs["data"], bytes)
        assert "json" not in trace_call.kwargs

    @patch("agentic_ci.mlflow.requests.post")
    def test_sends_valid_protobuf_body(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"experiments": [{"experiment_id": "42"}]}
        mock_post.return_value = mock_resp

        path = self._write_jsonl(
            [{"path": "/v1/traces", "payload": copy.deepcopy(HEX_TRACE_PAYLOAD)}]
        )
        try:
            push_traces(path, "http://mlflow:5000", "exp")
        finally:
            os.unlink(path)

        trace_call = mock_post.call_args_list[1]
        body = trace_call.kwargs["data"]
        parsed = ExportTraceServiceRequest()
        parsed.ParseFromString(body)
        assert parsed.resource_spans[0].scope_spans[0].spans[0].name == "test-span"
        assert parsed.resource_spans[0].scope_spans[0].spans[0].trace_id == bytes.fromhex(
            "0af7651916cd43dd8448eb211c80319c"
        )
