"""Tests for the OTLP HTTP/JSON collector."""

import json
import math
import urllib.request

import pytest

from agentic_ci.cost import estimate_cost
from agentic_ci.otel import (
    UsageSummary,
    _build_root_span_record,
    _find_orphan_traces,
    _has_any_traces,
    append_codex_usage,
    generate_trace_context,
    inject_root_spans,
    parse_metrics,
    print_summary,
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


def _write_jsonl(log_path, records):
    with open(log_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _otel_attr(key, value):
    if isinstance(value, bool):
        value_key = "boolValue"
    elif isinstance(value, int):
        value_key = "intValue"
    elif isinstance(value, float):
        value_key = "doubleValue"
    else:
        value_key = "stringValue"
    return {"key": key, "value": {value_key: value}}


def _codex_completed_log(model, input_tokens, output_tokens, cached=0, cache_write=0):
    attributes = {
        "event.name": "codex.sse_event",
        "event.kind": "response.completed",
        "model": model,
        "input_token_count": input_tokens,
        "cached_token_count": cached,
        "cache_write_token_count": cache_write,
        "output_token_count": output_tokens,
    }
    return {
        "ts": "2024-01-01T00:00:00+00:00",
        "path": "/v1/logs",
        "payload": {
            "resourceLogs": [
                {
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "attributes": [
                                        _otel_attr(key, value) for key, value in attributes.items()
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    }


def _named_metric_points(records, name):
    points = []
    for rec in records:
        if "/v1/metrics" not in rec.get("path", ""):
            continue
        payload = rec.get("payload") or {}
        for rm in payload.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for metric in sm.get("metrics", []):
                    if metric.get("name") == name:
                        data = metric.get("sum", {})
                        points.extend(data.get("dataPoints", []))
    return points


def _point_value(point):
    return point.get("asDouble", point.get("asInt"))


def _point_attr(point, key):
    for attribute in point.get("attributes", []):
        if attribute.get("key") == key:
            value = attribute.get("value", {})
            return value.get("stringValue", value.get("intValue"))
    return None


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


class TestParseMetrics:
    def test_parses_codex_token_usage_and_api_requests(self):
        attributes = {
            "event.name": "codex.sse_event",
            "event.kind": "response.completed",
            "model": "gpt-test",
            "input_token_count": "100",
            "cached_token_count": 40,
            "cache_write_token_count": 10,
            "output_token_count": "20",
        }

        def attr(key, value):
            value_key = "intValue" if isinstance(value, int) else "stringValue"
            return {"key": key, "value": {value_key: value}}

        response_record = {"attributes": [attr(key, value) for key, value in attributes.items()]}
        request_record = {
            "attributes": [
                attr("event.name", "codex.api_request"),
                attr("duration_ms", "123"),
            ]
        }
        records = [
            {
                "path": "/v1/logs",
                "payload": {
                    "resourceLogs": [
                        {
                            "scopeLogs": [
                                {
                                    "logRecords": [
                                        response_record,
                                        request_record,
                                    ]
                                }
                            ]
                        }
                    ]
                },
            }
        ]

        token_totals, _, api_requests, _ = parse_metrics(records)

        assert token_totals[("gpt-test", "input")] == 50
        assert token_totals[("gpt-test", "cacheRead")] == 40
        assert token_totals[("gpt-test", "cacheCreation")] == 10
        assert token_totals[("gpt-test", "output")] == 20
        assert api_requests == [{"event.name": "codex.api_request", "duration_ms": "123"}]

    def test_parses_codex_estimated_cost_and_response_request_fallback(self):
        attributes = {
            "event.name": "codex.sse_event",
            "event.kind": "response.completed",
            "model": "gpt-5.6-sol",
            "input_token_count": "100",
            "cached_token_count": 40,
            "cache_write_token_count": 10,
            "output_token_count": "20",
            "service_tier": "priority",
        }

        def attr(key, value):
            value_key = "intValue" if isinstance(value, int) else "stringValue"
            return {"key": key, "value": {value_key: value}}

        records = [
            {
                "path": "/v1/logs",
                "payload": {
                    "resourceLogs": [
                        {
                            "scopeLogs": [
                                {
                                    "scope": {"name": "codex"},
                                    "logRecords": [
                                        {
                                            "attributes": [
                                                attr(key, value)
                                                for key, value in attributes.items()
                                            ]
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
            }
        ]

        token_totals, cost_totals, api_requests, _ = parse_metrics(records)

        assert token_totals[("gpt-5.6-sol", "input")] == 50
        assert token_totals[("gpt-5.6-sol", "cacheRead")] == 40
        assert token_totals[("gpt-5.6-sol", "cacheCreation")] == 10
        assert token_totals[("gpt-5.6-sol", "output")] == 20
        assert cost_totals["gpt-5.6-sol"] == pytest.approx(0.001332)
        assert len(api_requests) == 1
        assert api_requests[0]["event.kind"] == "response.completed"

    def test_prefers_codex_reported_cost_over_estimate(self):
        records = [
            {
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
                                                    "value": {"stringValue": "codex.sse_event"},
                                                },
                                                {
                                                    "key": "event.kind",
                                                    "value": {"stringValue": "response.completed"},
                                                },
                                                {
                                                    "key": "model",
                                                    "value": {"stringValue": "gpt-5.6-sol"},
                                                },
                                                {
                                                    "key": "input_token_count",
                                                    "value": {"intValue": 10},
                                                },
                                                {
                                                    "key": "output_token_count",
                                                    "value": {"intValue": 10},
                                                },
                                                {
                                                    "key": "cost_usd",
                                                    "value": {"doubleValue": 0.25},
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
        ]

        _, cost_totals, _, _ = parse_metrics(records)

        assert cost_totals["gpt-5.6-sol"] == pytest.approx(0.25)

    def test_parses_protojson_int64_strings_and_skips_invalid_values(self):
        records = [
            {
                "path": "/v1/metrics",
                "payload": {
                    "resourceMetrics": [
                        {
                            "scopeMetrics": [
                                {
                                    "metrics": [
                                        {
                                            "name": "codex.token.usage",
                                            "sum": {
                                                "dataPoints": [
                                                    {
                                                        "asInt": "50",
                                                        "attributes": [
                                                            _otel_attr("model", "gpt-6.1-sol"),
                                                            _otel_attr("type", "input"),
                                                        ],
                                                    },
                                                    {
                                                        "asInt": "nope",
                                                        "attributes": [
                                                            _otel_attr("model", "gpt-6.1-sol"),
                                                            _otel_attr("type", "output"),
                                                        ],
                                                    },
                                                    {
                                                        "asDouble": "NaN",
                                                        "attributes": [
                                                            _otel_attr("model", "gpt-6.1-sol"),
                                                            _otel_attr("type", "cacheRead"),
                                                        ],
                                                    },
                                                ]
                                            },
                                        },
                                        {
                                            "name": "codex.cost.usage",
                                            "sum": {
                                                "dataPoints": [
                                                    {
                                                        "asDouble": "1.25",
                                                        "attributes": [
                                                            _otel_attr("model", "gpt-6.1-sol")
                                                        ],
                                                    },
                                                    {
                                                        "asInt": "bad",
                                                        "attributes": [
                                                            _otel_attr("model", "gpt-other")
                                                        ],
                                                    },
                                                ]
                                            },
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                },
            }
        ]

        token_totals, cost_totals, _, _ = parse_metrics(records)

        assert token_totals[("gpt-6.1-sol", "input")] == 50
        assert ("gpt-6.1-sol", "output") not in token_totals
        assert ("gpt-6.1-sol", "cacheRead") not in token_totals
        assert cost_totals["gpt-6.1-sol"] == pytest.approx(1.25)
        assert "gpt-other" not in cost_totals


class TestAppendCodexUsage:
    def test_appends_priced_codex_usage_without_double_counting(self, tmp_path):
        records = [
            _codex_completed_log("gpt-6.1-sol", 100, 20, cached=40, cache_write=10),
            _codex_completed_log("gpt-5.6-luna", 10, 5, cached=2, cache_write=1),
        ]
        token_totals, cost_totals, _, _ = parse_metrics(records)
        expected = estimate_cost(
            "gpt-6.1-sol",
            input_tokens=50,
            output_tokens=20,
            cached_input_tokens=40,
            cache_creation_input_tokens=10,
        )
        luna_expected = estimate_cost(
            "gpt-5.6-luna",
            input_tokens=7,
            output_tokens=5,
            cached_input_tokens=2,
            cache_creation_input_tokens=1,
        )
        assert expected is not None and expected > 0
        assert luna_expected is not None and luna_expected > 0
        assert cost_totals["gpt-6.1-sol"] == pytest.approx(expected)
        assert cost_totals["gpt-5.6-luna"] == pytest.approx(luna_expected)

        token_totals[("claude-opus-4-6", "input")] += 10
        cost_totals["claude-opus-4-6"] += 1.25

        log_path = tmp_path / "claude-otel.jsonl"
        _write_jsonl(log_path, records)
        assert append_codex_usage(log_path, records, token_totals, cost_totals)

        saved = _read_log(log_path)
        assert len(saved) == 3
        cost_points = _named_metric_points(saved, "codex.cost.usage")
        token_points = _named_metric_points(saved, "codex.token.usage")
        costs = {_point_attr(point, "model"): _point_value(point) for point in cost_points}
        models = {_point_attr(point, "model") for point in cost_points + token_points}
        assert costs["gpt-6.1-sol"] == pytest.approx(expected)
        assert costs["gpt-5.6-luna"] == pytest.approx(luna_expected)
        assert set(costs) == {"gpt-6.1-sol", "gpt-5.6-luna"}
        assert "claude-opus-4-6" not in models

        again_tokens, again_cost, _, _ = parse_metrics(saved)
        assert again_cost["gpt-6.1-sol"] == pytest.approx(expected)
        assert again_cost["gpt-5.6-luna"] == pytest.approx(luna_expected)
        assert again_tokens[("gpt-6.1-sol", "input")] == 50
        assert ("claude-opus-4-6", "input") not in again_tokens

        only_tokens, only_cost, _, _ = parse_metrics([saved[-1]])
        assert only_cost["gpt-6.1-sol"] == pytest.approx(expected)
        assert only_tokens[("gpt-6.1-sol", "cacheRead")] == 40

        inflated = json.loads(json.dumps(saved[-1]))
        for point in _named_metric_points([inflated], "codex.cost.usage"):
            if _point_attr(point, "model") == "gpt-6.1-sol":
                point.pop("asInt", None)
                point["asDouble"] = 50
        mixed_tokens, mixed_cost, _, _ = parse_metrics([inflated, *records])
        assert mixed_cost["gpt-6.1-sol"] == pytest.approx(expected)
        assert mixed_cost["gpt-5.6-luna"] == pytest.approx(luna_expected)
        assert mixed_tokens[("gpt-6.1-sol", "input")] == 50

    def test_dedup_keeps_a_model_that_has_no_events(self):
        records = [_codex_completed_log("gpt-6.1-sol", 100, 20, cached=40, cache_write=10)]
        token_totals, cost_totals, _, _ = parse_metrics(records)
        metric = {
            "path": "/v1/metrics",
            "payload": {
                "resourceMetrics": [
                    {
                        "scopeMetrics": [
                            {
                                "metrics": [
                                    {
                                        "name": "codex.cost.usage",
                                        "sum": {
                                            "dataPoints": [
                                                {
                                                    "asDouble": 50,
                                                    "attributes": [
                                                        _otel_attr("model", "gpt-6.1-sol")
                                                    ],
                                                },
                                                {
                                                    "asDouble": 1.5,
                                                    "attributes": [
                                                        _otel_attr("model", "gpt-5.6-luna")
                                                    ],
                                                },
                                            ]
                                        },
                                    },
                                    {
                                        "name": "codex.token.usage",
                                        "sum": {
                                            "dataPoints": [
                                                {
                                                    "asInt": 9,
                                                    "attributes": [
                                                        _otel_attr("model", "gpt-5.6-luna"),
                                                        _otel_attr("type", "output"),
                                                    ],
                                                }
                                            ]
                                        },
                                    },
                                ]
                            }
                        ]
                    }
                ]
            },
        }

        parsed_tokens, parsed_costs, _, _ = parse_metrics([metric, *records])

        assert parsed_costs["gpt-6.1-sol"] == pytest.approx(cost_totals["gpt-6.1-sol"])
        assert parsed_costs["gpt-5.6-luna"] == pytest.approx(1.5)
        assert parsed_tokens[("gpt-5.6-luna", "output")] == 9
        assert parsed_tokens[("gpt-6.1-sol", "input")] == token_totals[("gpt-6.1-sol", "input")]

    def test_unknown_codex_model_appends_tokens_without_cost(self, tmp_path):
        records = [_codex_completed_log("gpt-not-a-real-model", 30, 4, cached=5, cache_write=1)]
        token_totals, cost_totals, _, _ = parse_metrics(records)
        assert token_totals[("gpt-not-a-real-model", "input")] == 24
        assert "gpt-not-a-real-model" not in cost_totals

        log_path = tmp_path / "claude-otel.jsonl"
        _write_jsonl(log_path, records)
        assert append_codex_usage(log_path, records, token_totals, cost_totals)

        saved = _read_log(log_path)
        assert _named_metric_points(saved, "codex.cost.usage") == []
        token_points = _named_metric_points(saved, "codex.token.usage")
        counts = {
            _point_attr(point, "type"): _point_value(point)
            for point in token_points
            if _point_attr(point, "model") == "gpt-not-a-real-model"
        }
        assert counts == {"input": 24, "cacheRead": 5, "cacheCreation": 1, "output": 4}

        again_tokens, again_cost, _, _ = parse_metrics(saved)
        assert again_tokens[("gpt-not-a-real-model", "input")] == 24
        assert "gpt-not-a-real-model" not in again_cost

        only_tokens, only_cost, _, _ = parse_metrics([saved[-1]])
        assert only_tokens[("gpt-not-a-real-model", "output")] == 4
        assert "gpt-not-a-real-model" not in only_cost

    def test_skips_non_finite_and_negative_token_points(self, tmp_path):
        records = [_codex_completed_log("gpt-6.1-sol", 100, 20)]
        attributes = records[0]["payload"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0][
            "attributes"
        ]
        for attribute in attributes:
            if attribute["key"] == "output_token_count":
                attribute["value"] = {"stringValue": "NaN"}
        token_totals, cost_totals, _, _ = parse_metrics(records)
        assert math.isnan(token_totals[("gpt-6.1-sol", "output")])
        token_totals[("gpt-6.1-sol", "cacheRead")] = -5
        token_totals[("gpt-6.1-sol", "cacheCreation")] = float("inf")

        log_path = tmp_path / "claude-otel.jsonl"
        _write_jsonl(log_path, records)
        assert append_codex_usage(log_path, records, token_totals, cost_totals)

        appended = json.loads(log_path.read_text().splitlines()[-1])
        types = {
            _point_attr(point, "type")
            for point in _named_metric_points([appended], "codex.token.usage")
        }
        assert types == {"input"}

    def test_returns_false_when_every_point_is_unusable(self, tmp_path):
        records = [_codex_completed_log("gpt-not-a-real-model", 0, 0)]
        log_path = tmp_path / "claude-otel.jsonl"
        wrote = append_codex_usage(
            log_path,
            records,
            {("gpt-not-a-real-model", "output"): float("nan")},
            {"gpt-not-a-real-model": float("nan")},
        )
        assert wrote is False
        assert not log_path.exists()

    def test_append_skips_logs_without_codex_responses(self, tmp_path):
        records = [{"path": "/v1/metrics", "payload": {}}]
        log_path = tmp_path / "claude-otel.jsonl"
        wrote = append_codex_usage(
            log_path,
            records,
            {("claude-opus-4-6", "input"): 10},
            {"claude-opus-4-6": 1.25},
        )
        assert wrote is False
        assert not log_path.exists()


class TestPrintSummary:
    def test_missing_log(self, tmp_path, capsys):
        summary = print_summary(tmp_path / "missing.jsonl")

        assert summary == UsageSummary([], {}, {})
        assert "log file not found" in capsys.readouterr().out

    def test_invalid_json(self, tmp_path, capsys):
        log_path = tmp_path / "otel.jsonl"
        log_path.write_text("not-json\n")

        summary = print_summary(log_path)

        assert summary == UsageSummary([], {}, {})
        assert "Error parsing OTEL log" in capsys.readouterr().out

    def test_empty_log(self, tmp_path, capsys):
        log_path = tmp_path / "otel.jsonl"
        log_path.write_text("\n")

        summary = print_summary(log_path)

        assert summary == UsageSummary([], {}, {})
        assert "No OTEL data collected." in capsys.readouterr().out

    def test_returns_parsed_totals(self, tmp_path, capsys):
        log_path = tmp_path / "otel.jsonl"
        _write_jsonl(
            log_path,
            [_codex_completed_log("gpt-6.1-sol", 100, 20, cached=40, cache_write=10)],
        )

        records, token_totals, cost_totals = print_summary(log_path)

        assert len(records) == 1
        assert token_totals[("gpt-6.1-sol", "input")] == 50
        assert cost_totals["gpt-6.1-sol"] > 0
        assert "gpt-6.1-sol" in capsys.readouterr().out

    def test_summary_accepts_protojson_numbers_and_skips_invalid(self, tmp_path, capsys):
        log_path = tmp_path / "otel.jsonl"
        _write_jsonl(
            log_path,
            [
                {
                    "path": "/v1/metrics",
                    "payload": {
                        "resourceMetrics": [
                            {
                                "scopeMetrics": [
                                    {
                                        "metrics": [
                                            {
                                                "name": "codex.token.usage",
                                                "sum": {
                                                    "dataPoints": [
                                                        {
                                                            "asInt": "50",
                                                            "attributes": [
                                                                _otel_attr("model", "gpt-6.1-sol"),
                                                                _otel_attr("type", "input"),
                                                            ],
                                                        },
                                                        {
                                                            "asInt": "nope",
                                                            "attributes": [
                                                                _otel_attr("model", "gpt-6.1-sol"),
                                                                _otel_attr("type", "output"),
                                                            ],
                                                        },
                                                    ]
                                                },
                                            },
                                            {
                                                "name": "codex.cost.usage",
                                                "sum": {
                                                    "dataPoints": [
                                                        {
                                                            "asDouble": "1.5",
                                                            "attributes": [
                                                                _otel_attr("model", "gpt-6.1-sol")
                                                            ],
                                                        },
                                                        {
                                                            "asDouble": "NaN",
                                                            "attributes": [
                                                                _otel_attr("model", "gpt-other")
                                                            ],
                                                        },
                                                    ]
                                                },
                                            },
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                }
            ],
        )

        _, token_totals, cost_totals = print_summary(log_path)
        output = capsys.readouterr().out

        assert token_totals[("gpt-6.1-sol", "input")] == 50
        assert ("gpt-6.1-sol", "output") not in token_totals
        assert cost_totals["gpt-6.1-sol"] == pytest.approx(1.5)
        assert "gpt-other" not in cost_totals
        assert "50" in output
        assert "1.5000" in output

    def test_summary_omits_oversized_integers(self, tmp_path, capsys):
        log_path = tmp_path / "otel.jsonl"
        _write_jsonl(
            log_path,
            [
                {
                    "path": "/v1/metrics",
                    "payload": {
                        "resourceMetrics": [
                            {
                                "scopeMetrics": [
                                    {
                                        "metrics": [
                                            {
                                                "name": "codex.token.usage",
                                                "sum": {
                                                    "dataPoints": [
                                                        {
                                                            "asInt": 10**400,
                                                            "attributes": [
                                                                _otel_attr("model", "gpt-6.1-sol"),
                                                                _otel_attr("type", "input"),
                                                            ],
                                                        },
                                                        {
                                                            "asInt": "40",
                                                            "attributes": [
                                                                _otel_attr("model", "gpt-6.1-sol"),
                                                                _otel_attr("type", "output"),
                                                            ],
                                                        },
                                                    ]
                                                },
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                }
            ],
        )

        _, token_totals, _ = print_summary(log_path)

        assert ("gpt-6.1-sol", "input") not in token_totals
        assert token_totals[("gpt-6.1-sol", "output")] == 40
        assert "40" in capsys.readouterr().out


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

    def test_returns_dangling_parent_id(self):
        child1 = _make_span("aaaa" * 8, "1111" * 4, parent_span_id="dddd" * 4)
        child2 = _make_span("aaaa" * 8, "2222" * 4, parent_span_id="dddd" * 4)
        records = [_make_trace_record(child1, child2)]
        orphans = _find_orphan_traces(records)
        _, _, dangling = orphans["aaaa" * 8]
        assert dangling == "dddd" * 4

    def test_dangling_parent_most_common(self):
        child1 = _make_span("aaaa" * 8, "1111" * 4, parent_span_id="dddd" * 4)
        child2 = _make_span("aaaa" * 8, "2222" * 4, parent_span_id="dddd" * 4)
        child3 = _make_span("aaaa" * 8, "3333" * 4, parent_span_id="eeee" * 4)
        records = [_make_trace_record(child1, child2, child3)]
        orphans = _find_orphan_traces(records)
        _, _, dangling = orphans["aaaa" * 8]
        assert dangling == "dddd" * 4

    def test_malformed_timestamp_skipped(self):
        child = _make_span("aaaa" * 8, "cccc" * 4, parent_span_id="bbbb" * 4)
        child["startTimeUnixNano"] = "not-a-number"
        records = [_make_trace_record(child)]
        orphans = _find_orphan_traces(records)
        assert "aaaa" * 8 in orphans

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
        assert new_span["spanId"] == "bbbb" * 4
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

    def test_multiple_orphan_traces_are_stitched_under_one_root(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        child1 = _make_span("aaaa" * 8, "1111" * 4, parent_span_id="0000" * 4)
        child1b = _make_span("aaaa" * 8, "1212" * 4, parent_span_id="1111" * 4)
        child2 = _make_span("bbbb" * 8, "2222" * 4, parent_span_id="9999" * 4)
        with open(log_file, "w") as f:
            f.write(json.dumps(_make_trace_record(child1, child1b)) + "\n")
            f.write(json.dumps(_make_trace_record(child2)) + "\n")

        injected = inject_root_spans(log_file, 1000, 2000, exit_code=1)
        assert injected == 1

        spans = [
            sp
            for rec in _read_log(log_file)
            for rs in rec["payload"]["resourceSpans"]
            for ss in rs["scopeSpans"]
            for sp in ss["spans"]
        ]
        assert {sp["traceId"] for sp in spans} == {"aaaa" * 8}
        by_id = {sp["spanId"]: sp for sp in spans}
        # The larger trace wins; its dangling parent becomes the run root.
        assert not by_id["0000" * 4].get("parentSpanId")
        assert by_id["1111" * 4]["parentSpanId"] == "0000" * 4
        assert by_id["1212" * 4]["parentSpanId"] == "1111" * 4
        # The other trace's dangling span is re-parented under the run root.
        assert by_id["2222" * 4]["parentSpanId"] == "0000" * 4

    def test_complete_side_traces_are_stitched_into_traceparent_trace(self, tmp_path):
        """Codex starts auth/broker spans with no parent; they join the run trace."""
        log_file = str(tmp_path / "otel.jsonl")
        run_trace = "ffff" * 8
        run_span = "eeee" * 4
        main = _make_span(run_trace, "1111" * 4, parent_span_id=run_span)
        lost_parent = _make_span(run_trace, "1212" * 4, parent_span_id="dead" * 4)
        auth = _make_span("bbbb" * 8, "2222" * 4)
        broker = _make_span("cccc" * 8, "3333" * 4)
        broker_child = _make_span("cccc" * 8, "3434" * 4, parent_span_id="3333" * 4)
        log_rec = {
            "path": "/v1/logs",
            "payload": {
                "resourceLogs": [
                    {"scopeLogs": [{"logRecords": [{"traceId": "cccc" * 8, "body": {}}]}]}
                ]
            },
        }
        with open(log_file, "w") as f:
            f.write(json.dumps(_make_trace_record(main, lost_parent)) + "\n")
            f.write(json.dumps(_make_trace_record(auth)) + "\n")
            f.write(json.dumps(_make_trace_record(broker, broker_child)) + "\n")
            f.write(json.dumps(log_rec) + "\n")

        injected = inject_root_spans(
            log_file,
            1000,
            2000,
            exit_code=0,
            fallback_trace_id=run_trace,
            fallback_span_id=run_span,
        )
        assert injected == 1

        records = _read_log(log_file)
        spans = [
            sp
            for rec in records
            if "resourceSpans" in rec["payload"]
            for rs in rec["payload"]["resourceSpans"]
            for ss in rs["scopeSpans"]
            for sp in ss["spans"]
        ]
        assert {sp["traceId"] for sp in spans} == {run_trace}
        by_id = {sp["spanId"]: sp for sp in spans}
        assert not by_id[run_span].get("parentSpanId")  # synthetic run root
        assert by_id["1212" * 4]["parentSpanId"] == run_span  # lost parent re-homed
        assert by_id["2222" * 4]["parentSpanId"] == run_span
        assert by_id["3333" * 4]["parentSpanId"] == run_span
        assert by_id["3434" * 4]["parentSpanId"] == "3333" * 4  # intra-trace parent kept
        logs = [
            lr
            for rec in records
            if "resourceLogs" in rec["payload"]
            for rl in rec["payload"]["resourceLogs"]
            for sl in rl["scopeLogs"]
            for lr in sl["logRecords"]
        ]
        assert logs[0]["traceId"] == run_trace

    def test_second_missing_parent_is_rehomed_under_synthetic_root(self, tmp_path):
        """Codex drops its exec root and a session span; both sets of children
        must end up under the one synthetic root."""
        log_file = str(tmp_path / "otel.jsonl")
        a1 = _make_span("aaaa" * 8, "1111" * 4, parent_span_id="dead" * 4)
        a2 = _make_span("aaaa" * 8, "2222" * 4, parent_span_id="dead" * 4)
        b1 = _make_span("aaaa" * 8, "3333" * 4, parent_span_id="beef" * 4)
        b1_child = _make_span("aaaa" * 8, "3434" * 4, parent_span_id="3333" * 4)
        with open(log_file, "w") as f:
            f.write(json.dumps(_make_trace_record(a1, a2, b1, b1_child)) + "\n")

        assert inject_root_spans(log_file, 1000, 2000, exit_code=0) == 1

        spans = [
            sp
            for rec in _read_log(log_file)
            for rs in rec["payload"]["resourceSpans"]
            for ss in rs["scopeSpans"]
            for sp in ss["spans"]
        ]
        by_id = {sp["spanId"]: sp for sp in spans}
        assert not by_id["dead" * 4].get("parentSpanId")  # majority parent became root
        assert by_id["1111" * 4]["parentSpanId"] == "dead" * 4
        assert by_id["3333" * 4]["parentSpanId"] == "dead" * 4  # re-homed
        assert by_id["3434" * 4]["parentSpanId"] == "3333" * 4  # kept
        ids = set(by_id)
        assert all(not sp.get("parentSpanId") or sp["parentSpanId"] in ids for sp in spans)

    def test_dangling_spans_in_rooted_trace_are_rehomed_under_real_root(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        root = _make_span("aaaa" * 8, "1111" * 4)
        child = _make_span("aaaa" * 8, "2222" * 4, parent_span_id="1111" * 4)
        lost = _make_span("aaaa" * 8, "3333" * 4, parent_span_id="dead" * 4)
        with open(log_file, "w") as f:
            f.write(json.dumps(_make_trace_record(root, child, lost)) + "\n")

        assert inject_root_spans(log_file, 1000, 2000, exit_code=0) == 0

        spans = [
            sp
            for rec in _read_log(log_file)
            for rs in rec["payload"]["resourceSpans"]
            for ss in rs["scopeSpans"]
            for sp in ss["spans"]
        ]
        by_id = {sp["spanId"]: sp for sp in spans}
        assert len(spans) == 3
        assert not by_id["1111" * 4].get("parentSpanId")
        assert by_id["2222" * 4]["parentSpanId"] == "1111" * 4
        assert by_id["3333" * 4]["parentSpanId"] == "1111" * 4

    def test_single_trace_is_left_untouched(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        root = _make_span("aaaa" * 8, "1111" * 4)
        child = _make_span("aaaa" * 8, "2222" * 4, parent_span_id="1111" * 4)
        original = json.dumps(_make_trace_record(root, child))
        with open(log_file, "w") as f:
            f.write(original + "\n")

        assert inject_root_spans(log_file, 1000, 2000, exit_code=0) == 0
        with open(log_file) as f:
            assert f.read() == original + "\n"

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

    def test_timing_expands_to_cover_child(self, tmp_path):
        log_file = str(tmp_path / "otel.jsonl")
        child = _make_span("aaaa" * 8, "cccc" * 4, parent_span_id="bbbb" * 4)
        child["startTimeUnixNano"] = "500"
        child["endTimeUnixNano"] = "12000"
        record = _make_trace_record(child)
        with open(log_file, "w") as f:
            f.write(json.dumps(record) + "\n")

        inject_root_spans(log_file, 1000, 9000, exit_code=0)

        records = _read_log(log_file)
        span = records[1]["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert int(span["startTimeUnixNano"]) == 500
        assert int(span["endTimeUnixNano"]) == 12000


class TestParseMetricsRobustness:
    """parse_metrics must tolerate malformed OTLP attribute entries."""

    def test_metrics_attribute_missing_key_is_skipped(self):
        records = [
            {
                "path": "/v1/metrics",
                "payload": {
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
                                                        "asInt": 5,
                                                        "attributes": [
                                                            {"value": {"stringValue": "orphan"}},
                                                            {
                                                                "key": "model",
                                                                "value": {"stringValue": "gpt-5"},
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
                },
            }
        ]
        token_totals, _, _, _ = parse_metrics(records)
        assert token_totals[("gpt-5", "input")] == 5

    def test_logs_attribute_missing_key_is_skipped(self):
        records = [
            {
                "path": "/v1/logs",
                "payload": {
                    "resourceLogs": [
                        {
                            "scopeLogs": [
                                {
                                    "logRecords": [
                                        {
                                            "attributes": [
                                                {"value": {"stringValue": "orphan"}},
                                                {
                                                    "key": "event.name",
                                                    "value": {"stringValue": "codex.api_request"},
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
        ]
        _, _, api_requests, _ = parse_metrics(records)
        assert len(api_requests) == 1
        assert api_requests[0]["event.name"] == "codex.api_request"
