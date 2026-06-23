"""Push OTel trace payloads from a JSONL log to an MLflow OTLP endpoint."""

import base64
import copy
import json
import sys

import requests
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

_ID_KEYS = ("traceId", "spanId", "parentSpanId")


def _hex_to_base64(value):
    """Convert a hex string to base64 for protobuf bytes fields."""
    if not isinstance(value, str) or len(value) not in (16, 32):
        return value
    try:
        return base64.b64encode(bytes.fromhex(value)).decode()
    except ValueError:
        return value


def _fixup_ids(payload):
    """Convert hex-encoded traceId/spanId/parentSpanId to base64.

    OTLP JSON spec uses lowercase hex for bytes fields, but protobuf's
    ParseDict expects base64 encoding.
    """
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for key in _ID_KEYS:
                    if key in span:
                        span[key] = _hex_to_base64(span[key])
                for link in span.get("links", []):
                    for key in _ID_KEYS:
                        if key in link:
                            link[key] = _hex_to_base64(link[key])
    return payload


def _resolve_experiment_id(endpoint, name, headers):
    """Look up an MLflow experiment by name. Returns ID or None."""
    url = f"{endpoint}/api/2.0/mlflow/experiments/search"
    escaped_name = name.replace("'", "''")
    try:
        resp = requests.post(
            url,
            json={"filter": f"name = '{escaped_name}'", "max_results": 1},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        experiments = resp.json().get("experiments", [])
        if experiments:
            return experiments[0]["experiment_id"]
    except requests.RequestException as e:
        detail = ""
        if hasattr(e, "response") and e.response is not None:
            detail = f" ({e.response.status_code}: {e.response.text[:200]})"
        print(f"Experiment lookup failed{detail}: {e}", file=sys.stderr)
    return None


def _serialize_traces(payload):
    """Convert an OTLP JSON trace payload dict to protobuf bytes."""
    payload = _fixup_ids(copy.deepcopy(payload))
    request = ExportTraceServiceRequest()
    json_format.ParseDict(payload, request)
    return request.SerializeToString()


def push_traces(log_file, endpoint, experiment, token=None):
    """Push /v1/traces records from a JSONL log to an MLflow OTLP endpoint.

    Returns (ok_count, error_count).
    """
    endpoint = endpoint.rstrip("/")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    records = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if "/v1/traces" in rec.get("path", ""):
                        records.append(rec)
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        return 0, 0

    if not records:
        return 0, 0

    experiment_id = _resolve_experiment_id(endpoint, experiment, headers)
    if not experiment_id:
        print(
            f"MLflow experiment '{experiment}' not found — skipping trace push.",
            file=sys.stderr,
        )
        return 0, 0

    traces_url = f"{endpoint}/v1/traces"
    ok, err = 0, 0

    for rec in records:
        payload = rec.get("payload")
        if not payload:
            continue
        try:
            data = _serialize_traces(payload)
            resp = requests.post(
                traces_url,
                data=data,
                headers={
                    **headers,
                    "Content-Type": "application/x-protobuf",
                    "x-mlflow-experiment-id": experiment_id,
                },
                timeout=30,
            )
            resp.raise_for_status()
            ok += 1
        except requests.RequestException as e:
            detail = ""
            if hasattr(e, "response") and e.response is not None:
                detail = f" ({e.response.status_code})"
            print(f"Trace push failed{detail}: {e}", file=sys.stderr)
            err += 1

    return ok, err
