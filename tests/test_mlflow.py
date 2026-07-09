"""Tests for agentic_ci.mlflow -- OTLP JSON trace push to MLflow."""

import copy
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import requests

from agentic_ci.mlflow import (
    PushResult,
    _add_query_source,
    _add_span_costs,
    _add_token_usage,
    _cost_by_session,
    _extract_session_ids,
    _extract_trace_ids,
    _finalize_traces,
    _prepare_payload,
    _query_source_by_request,
    push_traces,
)

TRACE_PAYLOAD = {
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


class TestPreparePayload:
    def test_deep_copies(self):
        payload = copy.deepcopy(TRACE_PAYLOAD)
        original_id = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]
        result = _prepare_payload(payload)
        assert result is not payload
        assert payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] == original_id

    def test_adds_token_usage(self):
        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "name": "claude_code.llm_request",
                                    "attributes": [
                                        {"key": "input_tokens", "value": {"intValue": "10"}},
                                        {"key": "output_tokens", "value": {"intValue": "20"}},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        result = _prepare_payload(payload)
        attrs = {
            a["key"]: a["value"]
            for a in result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert "mlflow.chat.tokenUsage" in attrs
        assert "gen_ai.usage.input_tokens" in attrs


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
        assert _chat_usage(payload) == {
            "input_tokens": 3,
            "output_tokens": 148,
            "total_tokens": 151,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 39565,
        }
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
        ok, err, _, _ = push_traces("/nonexistent.jsonl", "http://mlflow:5000", "exp")
        assert (ok, err) == (0, 0)

    def test_no_trace_records_returns_zero(self):
        path = self._write_jsonl([{"path": "/v1/metrics", "payload": {}}])
        try:
            ok, err, _, _ = push_traces(path, "http://mlflow:5000", "exp")
            assert (ok, err) == (0, 0)
        finally:
            os.unlink(path)

    @patch("agentic_ci.mlflow.requests.get")
    @patch("agentic_ci.mlflow.requests.post")
    def test_sends_json_content_type(self, mock_post, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"experiments": [{"experiment_id": "123"}]}
        mock_post.return_value = mock_resp
        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status = MagicMock()
        mock_get_resp.json.return_value = {"traces": []}
        mock_get.return_value = mock_get_resp

        path = self._write_jsonl([{"path": "/v1/traces", "payload": copy.deepcopy(TRACE_PAYLOAD)}])
        try:
            ok, err, _, _ = push_traces(path, "http://mlflow:5000", "exp")
        finally:
            os.unlink(path)

        assert ok == 1
        assert err == 0

        trace_call = mock_post.call_args_list[1]
        assert trace_call.kwargs["headers"]["x-mlflow-experiment-id"] == "123"
        assert "json" in trace_call.kwargs
        assert "data" not in trace_call.kwargs

    @patch("agentic_ci.mlflow.requests.get")
    @patch("agentic_ci.mlflow.requests.post")
    def test_sends_valid_json_body(self, mock_post, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"experiments": [{"experiment_id": "42"}]}
        mock_post.return_value = mock_resp
        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status = MagicMock()
        mock_get_resp.json.return_value = {"traces": []}
        mock_get.return_value = mock_get_resp

        path = self._write_jsonl([{"path": "/v1/traces", "payload": copy.deepcopy(TRACE_PAYLOAD)}])
        try:
            push_traces(path, "http://mlflow:5000", "exp")
        finally:
            os.unlink(path)

        trace_call = mock_post.call_args_list[1]
        body = trace_call.kwargs["json"]
        assert "resourceSpans" in body
        span = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "test-span"
        assert span["traceId"] == "0af7651916cd43dd8448eb211c80319c"

    @patch("agentic_ci.mlflow.requests.get")
    @patch("agentic_ci.mlflow.requests.post")
    def test_returns_trace_and_session_ids(self, mock_post, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"experiments": [{"experiment_id": "1"}]}
        mock_post.return_value = mock_resp
        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status = MagicMock()
        mock_get_resp.json.return_value = {"traces": []}
        mock_get.return_value = mock_get_resp

        payload = copy.deepcopy(TRACE_PAYLOAD)
        payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
            {"key": "session.id", "value": {"stringValue": "sess-abc"}}
        )
        path = self._write_jsonl([{"path": "/v1/traces", "payload": payload}])
        try:
            ok, err, trace_ids, session_ids = push_traces(path, "http://mlflow:5000", "exp")
        finally:
            os.unlink(path)

        assert ok == 1
        assert err == 0
        assert "tr-0af7651916cd43dd8448eb211c80319c" in trace_ids
        assert "sess-abc" in session_ids

    def test_missing_file_returns_empty_ids(self):
        ok, err, trace_ids, session_ids = push_traces(
            "/nonexistent.jsonl", "http://mlflow:5000", "exp"
        )
        assert (ok, err) == (0, 0)
        assert trace_ids == []
        assert session_ids == []


class TestFinalizeTraces:
    @patch("agentic_ci.mlflow.requests.patch")
    @patch("agentic_ci.mlflow.requests.get")
    def test_finalizes_in_progress_trace(self, mock_get, mock_patch):
        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status = MagicMock()
        mock_get_resp.json.return_value = {
            "traces": [{"request_id": "tr-abc", "status": "IN_PROGRESS"}]
        }
        mock_get.return_value = mock_get_resp
        mock_patch_resp = MagicMock()
        mock_patch_resp.raise_for_status = MagicMock()
        mock_patch.return_value = mock_patch_resp

        count = _finalize_traces("http://mlflow:5000", {}, ["tr-abc"])
        assert count == 1
        mock_patch.assert_called_once()
        call_json = mock_patch.call_args.kwargs["json"]
        assert call_json["status"] == "ERROR"

    @patch("agentic_ci.mlflow.requests.get")
    def test_skips_ok_trace(self, mock_get):
        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status = MagicMock()
        mock_get_resp.json.return_value = {"traces": [{"request_id": "tr-abc", "status": "OK"}]}
        mock_get.return_value = mock_get_resp

        count = _finalize_traces("http://mlflow:5000", {}, ["tr-abc"])
        assert count == 0

    @patch("agentic_ci.mlflow.requests.get")
    def test_skips_missing_trace(self, mock_get):
        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status = MagicMock()
        mock_get_resp.json.return_value = {"traces": []}
        mock_get.return_value = mock_get_resp

        count = _finalize_traces("http://mlflow:5000", {}, ["tr-abc"])
        assert count == 0

    @patch("agentic_ci.mlflow.requests.get")
    def test_handles_request_error(self, mock_get):
        mock_get.side_effect = requests.RequestException("timeout")
        count = _finalize_traces("http://mlflow:5000", {}, ["tr-abc"])
        assert count == 0

    def test_empty_trace_ids(self):
        count = _finalize_traces("http://mlflow:5000", {}, [])
        assert count == 0


class TestExtractTraceIds:
    def test_extracts_unique_trace_ids_with_prefix(self):
        payloads = [
            {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
                                "spans": [
                                    {"traceId": "abc123"},
                                    {"traceId": "abc123"},
                                    {"traceId": "def456"},
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
        result = _extract_trace_ids(payloads)
        assert result == ["tr-abc123", "tr-def456"]

    def test_empty_payloads(self):
        assert _extract_trace_ids([]) == []
        assert _extract_trace_ids([{}]) == []


class TestExtractSessionIds:
    def test_extracts_from_span_attributes(self):
        payloads = [
            {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "attributes": [
                                            {
                                                "key": "session.id",
                                                "value": {"stringValue": "sess-1"},
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
        result = _extract_session_ids(payloads, [])
        assert result == ["sess-1"]

    def test_extracts_from_metric_records(self):
        metric_rec = {
            "payload": {
                "resourceMetrics": [
                    {
                        "scopeMetrics": [
                            {
                                "metrics": [
                                    {
                                        "name": "claude_code.cost.usage",
                                        "sum": {
                                            "dataPoints": [
                                                {
                                                    "attributes": [
                                                        {
                                                            "key": "session.id",
                                                            "value": {"stringValue": "sess-m1"},
                                                        }
                                                    ]
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
        }
        result = _extract_session_ids([], [metric_rec])
        assert result == ["sess-m1"]

    def test_deduplicates_across_sources(self):
        payloads = [
            {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "attributes": [
                                            {
                                                "key": "session.id",
                                                "value": {"stringValue": "sess-x"},
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
        metric_rec = {
            "payload": {
                "resourceMetrics": [
                    {
                        "scopeMetrics": [
                            {
                                "metrics": [
                                    {
                                        "sum": {
                                            "dataPoints": [
                                                {
                                                    "attributes": [
                                                        {
                                                            "key": "session.id",
                                                            "value": {"stringValue": "sess-x"},
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
        }
        result = _extract_session_ids(payloads, [metric_rec])
        assert result == ["sess-x"]

    def test_extracts_from_histogram_metric(self):
        metric_rec = {
            "payload": {
                "resourceMetrics": [
                    {
                        "scopeMetrics": [
                            {
                                "metrics": [
                                    {
                                        "name": "claude_code.session.token.total",
                                        "histogram": {
                                            "dataPoints": [
                                                {
                                                    "attributes": [
                                                        {
                                                            "key": "session.id",
                                                            "value": {"stringValue": "sess-hist"},
                                                        }
                                                    ]
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
        }
        result = _extract_session_ids([], [metric_rec])
        assert result == ["sess-hist"]


class TestPushResult:
    def test_attribute_access(self):
        r = PushResult(ok=1, err=2, trace_ids=["t1"], session_ids=["s1"])
        assert r.ok == 1
        assert r.err == 2
        assert r.trace_ids == ["t1"]
        assert r.session_ids == ["s1"]

    def test_tuple_unpacking(self):
        ok, err, tids, sids = PushResult(3, 0, ["a", "b"], ["x"])
        assert ok == 3
        assert err == 0
        assert tids == ["a", "b"]
        assert sids == ["x"]
