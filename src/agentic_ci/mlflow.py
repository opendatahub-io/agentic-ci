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


# Claude Code emits bare token attributes (input_tokens, output_tokens,
# cache_*_tokens) that match no OTEL semantic convention MLflow recognizes.
# MLflow's OTLP ingestion derives per-span token usage — and from it the
# trace-level mlflow.trace.tokenUsage and cost that feed the experiment
# dashboard charts — only from namespaced conventions like these.
_GENAI_INPUT_KEY = "gen_ai.usage.input_tokens"
_GENAI_OUTPUT_KEY = "gen_ai.usage.output_tokens"
# Components folded into the GenAI input count. Claude's bare input_tokens
# excludes cached tokens, which usually dominate, so folding them in keeps the
# token-usage chart representative of what the model actually processed.
_INPUT_TOKEN_KEYS = ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
_OUTPUT_TOKEN_KEY = "output_tokens"


def _attr_int(value):
    """Extract an int from an OTLP JSON attribute value dict, or None."""
    if not isinstance(value, dict):
        return None
    for key in ("intValue", "int_value", "stringValue", "string_value"):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return None
    return None


def _add_genai_token_usage(payload):
    """Map Claude Code's bare token attributes onto the OTEL GenAI semconv.

    MLflow's OTLP translators recognize ``gen_ai.usage.input_tokens`` /
    ``gen_ai.usage.output_tokens`` and use them to populate the per-span token
    usage, the aggregated ``mlflow.trace.tokenUsage``, and the cost (via
    MLflow's pricing table) that drive the experiment-level token and cost
    charts. Claude Code instead emits bare ``input_tokens`` / ``output_tokens``
    / ``cache_*_tokens``, which match nothing — so without this mapping those
    charts stay empty even though the data is present on the spans.

    The GenAI input count folds ``cache_read`` + ``cache_creation`` into
    ``input_tokens`` so the chart reflects the total tokens processed. Cost is
    driven separately by :func:`_add_span_costs` from Claude's reported
    ``/v1/metrics`` spend; only when that is absent does MLflow fall back to
    pricing the folded input at the standard rate (approximate, over-counting
    cheap cache reads).

    Existing ``gen_ai.usage.*`` attributes are left untouched.
    """
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                attrs = span.get("attributes")
                if not isinstance(attrs, list):
                    continue
                by_key = {a.get("key"): a.get("value") for a in attrs if isinstance(a, dict)}
                if _GENAI_INPUT_KEY in by_key or _GENAI_OUTPUT_KEY in by_key:
                    continue
                inp = sum(_attr_int(by_key.get(k)) or 0 for k in _INPUT_TOKEN_KEYS)
                out = _attr_int(by_key.get(_OUTPUT_TOKEN_KEY)) or 0
                # MLflow only records usage when both input and output are > 0.
                if inp <= 0 or out <= 0:
                    continue
                attrs.append({"key": _GENAI_INPUT_KEY, "value": {"intValue": str(inp)}})
                attrs.append({"key": _GENAI_OUTPUT_KEY, "value": {"intValue": str(out)}})
    return payload


# Claude reports exact spend as a delta-temporality OTEL metric
# (claude_code.cost.usage, tagged session.id) in the /v1/metrics records —
# never as a span attribute. To drive MLflow's experiment cost chart, that
# total is distributed across the session's LLM spans as mlflow.llm.cost,
# which MLflow aggregates into mlflow.trace.cost. A pre-set mlflow.llm.cost is
# used verbatim (MLflow does not recompute it), so this yields the exact billed
# total rather than MLflow's token-derived approximation.
_COST_METRIC = "claude_code.cost.usage"
_SESSION_ID_KEY = "session.id"
_LLM_COST_KEY = "mlflow.llm.cost"


def _value_str(value):
    """Return the string content of an OTLP attribute value dict, or None."""
    if isinstance(value, dict):
        return value.get("stringValue", value.get("string_value"))
    return None


def _cost_by_session(metric_records):
    """Sum claude_code.cost.usage per session.id across /v1/metrics records.

    The metric is delta temporality, so the session total is a plain sum of
    its data points.
    """
    totals = {}
    for rec in metric_records:
        payload = rec.get("payload") or {}
        for rm in payload.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for metric in sm.get("metrics", []):
                    if metric.get("name") != _COST_METRIC:
                        continue
                    for dp in metric.get("sum", {}).get("dataPoints", []):
                        sid = next(
                            (
                                _value_str(a.get("value"))
                                for a in dp.get("attributes", [])
                                if isinstance(a, dict) and a.get("key") == _SESSION_ID_KEY
                            ),
                            None,
                        )
                        value = dp.get("asDouble", dp.get("asInt"))
                        if sid is None or value is None:
                            continue
                        totals[sid] = totals.get(sid, 0.0) + float(value)
    return totals


def _add_span_costs(payloads, cost_by_session):
    """Distribute each session's reported cost across its LLM spans.

    Spans are weighted by token volume (input + output + cache). The per-span
    share is written as mlflow.llm.cost so MLflow aggregates the exact session
    total into mlflow.trace.cost. Only the total is authoritative (the metric
    carries no input/output split); the in/out split is apportioned by tokens
    so the values stay self-consistent. Existing mlflow.llm.cost is preserved.
    """
    if not cost_by_session:
        return
    buckets = {}  # session id -> list of (attrs, in_weight, out_weight)
    weights = {}  # session id -> total token weight
    for payload in payloads:
        for rs in payload.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    attrs = span.get("attributes")
                    if not isinstance(attrs, list):
                        continue
                    by_key = {a.get("key"): a.get("value") for a in attrs if isinstance(a, dict)}
                    sid = _value_str(by_key.get(_SESSION_ID_KEY))
                    if sid is None or sid not in cost_by_session:
                        continue
                    in_w = sum(_attr_int(by_key.get(k)) or 0 for k in _INPUT_TOKEN_KEYS)
                    out_w = _attr_int(by_key.get(_OUTPUT_TOKEN_KEY)) or 0
                    if in_w + out_w <= 0:
                        continue
                    buckets.setdefault(sid, []).append((attrs, in_w, out_w))
                    weights[sid] = weights.get(sid, 0) + in_w + out_w
    for sid, spans in buckets.items():
        total_cost = cost_by_session[sid]
        total_weight = weights[sid]
        if total_cost <= 0 or total_weight <= 0:
            continue
        for attrs, in_w, out_w in spans:
            if any(isinstance(a, dict) and a.get("key") == _LLM_COST_KEY for a in attrs):
                continue
            share = total_cost * (in_w + out_w) / total_weight
            in_cost = share * in_w / (in_w + out_w)
            cost = {"input_cost": in_cost, "output_cost": share - in_cost, "total_cost": share}
            attrs.append({"key": _LLM_COST_KEY, "value": {"stringValue": json.dumps(cost)}})


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
    payload = _add_genai_token_usage(_fixup_ids(copy.deepcopy(payload)))
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

    trace_records = []
    metric_records = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                path = rec.get("path", "")
                if "/v1/traces" in path:
                    trace_records.append(rec)
                elif "/v1/metrics" in path:
                    metric_records.append(rec)
    except FileNotFoundError:
        return 0, 0

    if not trace_records:
        return 0, 0

    experiment_id = _resolve_experiment_id(endpoint, experiment, headers)
    if not experiment_id:
        print(
            f"MLflow experiment '{experiment}' not found — skipping trace push.",
            file=sys.stderr,
        )
        return 0, 0

    payloads = [rec["payload"] for rec in trace_records if rec.get("payload")]
    # Annotate spans with Claude's reported cost before serialization.
    _add_span_costs(payloads, _cost_by_session(metric_records))

    traces_url = f"{endpoint}/v1/traces"
    ok, err = 0, 0

    for payload in payloads:
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
