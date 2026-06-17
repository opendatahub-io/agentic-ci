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

from agentic_ci.mlflow import _fixup_ids, _hex_to_base64, _serialize_traces, push_traces

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
