"""Push OTel trace payloads from a JSONL log to an MLflow OTLP endpoint."""

import base64
import copy
import json
import sys
from typing import NamedTuple

import requests
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Status

_ID_KEYS = ("traceId", "spanId", "parentSpanId")

_SPAN_STATUS_ERROR = Status.StatusCode.STATUS_CODE_ERROR


def _latest_end_in_payload(payload):
    """Find the latest endTimeUnixNano across all complete spans in a payload."""
    latest = 0
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                end = span.get("endTimeUnixNano")
                if end and str(end) != "0":
                    latest = max(latest, int(end))
    return latest


def _close_incomplete_spans(payload):
    """Close spans that have no end time (killed agent, OOM, timeout).

    When Claude Code is killed mid-run, the OTEL batch processor cannot flush
    the root span. MLflow marks such traces "In Progress" permanently.

    For each incomplete span, endTimeUnixNano is set to the latest end time
    of any complete sibling/descendant span in the payload (best approximation
    of when the agent was still alive), falling back to startTimeUnixNano if
    no complete spans exist. Status is set to ERROR.
    """
    latest_end = _latest_end_in_payload(payload)
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                end = span.get("endTimeUnixNano")
                if not end or str(end) == "0":
                    start = int(span.get("startTimeUnixNano", "0"))
                    best_end = str(max(start, latest_end) if latest_end else start)
                    span["endTimeUnixNano"] = best_end
                    span["status"] = {
                        "code": _SPAN_STATUS_ERROR,
                        "message": "end time missing at push; span marked as error",
                    }
    return payload


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


# Claude Code emits per-call token counts as bare span attributes
# (input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens) — it
# does not emit the gen_ai.usage.* convention nor MLflow's own usage attribute.
# From those bare counts we write two things (see _add_token_usage):
#   - gen_ai.usage.input_tokens / output_tokens — the OTEL GenAI standard, for
#     any non-MLflow backend that reads the convention.
#   - mlflow.chat.tokenUsage — MLflow's native attribute, which it aggregates
#     into mlflow.trace.tokenUsage for the experiment Usage dashboard; this one
#     carries the cache breakdown (Cache Read / Cache Write) that gen_ai can't.
_GENAI_INPUT_KEY = "gen_ai.usage.input_tokens"
_GENAI_OUTPUT_KEY = "gen_ai.usage.output_tokens"
_CHAT_USAGE_KEY = "mlflow.chat.tokenUsage"
_INPUT_TOKEN_KEY = "input_tokens"
_OUTPUT_TOKEN_KEY = "output_tokens"
_CACHE_READ_KEY = "cache_read_tokens"
_CACHE_CREATION_KEY = "cache_creation_tokens"
# Token components that count toward a span's cost-distribution weight.
_WEIGHT_INPUT_KEYS = (_INPUT_TOKEN_KEY, _CACHE_READ_KEY, _CACHE_CREATION_KEY)


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


def _add_token_usage(payload):
    """Translate Claude's bare token counts into both the OTEL GenAI standard
    and MLflow's native usage attribute.

    Claude Code emits bare input_tokens / output_tokens / cache_read_tokens /
    cache_creation_tokens (input_tokens is *fresh*, non-cached) — never the
    gen_ai.usage.* convention or MLflow's own attribute. We synthesize both per
    LLM span:

    - ``gen_ai.usage.input_tokens`` / ``output_tokens`` — the OTEL GenAI
      convention (fresh input + output), so non-MLflow backends see standard
      usage. The convention has no cache field, so cache volume isn't there.
    - ``mlflow.chat.tokenUsage`` — MLflow's native attribute in its disjoint
      cache schema, which MLflow aggregates into ``mlflow.trace.tokenUsage``:
          input_tokens                 fresh input
          output_tokens                generated
          total_tokens                 input + output (cache excluded)
          cache_read_input_tokens      -> dashboard "Cache Read"
          cache_creation_input_tokens  -> dashboard "Cache Write"
      MLflow uses a pre-set value verbatim, so all four lines show on the
      experiment Usage dashboard.

    Cost is set separately by :func:`_add_span_costs` from Claude's reported
    /v1/metrics spend. We deliberately do not rely on MLflow's cache-aware
    auto-cost: fed Anthropic's disjoint counts it assumes prompt_tokens
    includes the cached tokens and can go negative. (That fallback only runs
    when no cost metric is present, so mlflow.llm.cost is left unset.)

    Existing values for any of these attributes are left untouched.
    """
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                attrs = span.get("attributes")
                if not isinstance(attrs, list):
                    continue
                by_key = {a.get("key"): a.get("value") for a in attrs if isinstance(a, dict)}
                inp = _attr_int(by_key.get(_INPUT_TOKEN_KEY)) or 0
                out = _attr_int(by_key.get(_OUTPUT_TOKEN_KEY)) or 0
                cache_read = _attr_int(by_key.get(_CACHE_READ_KEY)) or 0
                cache_creation = _attr_int(by_key.get(_CACHE_CREATION_KEY)) or 0
                counts = (inp, out, cache_read, cache_creation)
                # Skip malformed (negative) counts; require some positive usage.
                if any(c < 0 for c in counts) or sum(counts) <= 0:
                    continue
                # OTEL GenAI standard (fresh input + output; no cache field).
                if _GENAI_INPUT_KEY not in by_key:
                    attrs.append({"key": _GENAI_INPUT_KEY, "value": {"intValue": str(inp)}})
                if _GENAI_OUTPUT_KEY not in by_key:
                    attrs.append({"key": _GENAI_OUTPUT_KEY, "value": {"intValue": str(out)}})
                # MLflow native usage (disjoint, with cache lines).
                if _CHAT_USAGE_KEY not in by_key:
                    usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
                    if cache_read:
                        usage["cache_read_input_tokens"] = cache_read
                    if cache_creation:
                        usage["cache_creation_input_tokens"] = cache_creation
                    attrs.append(
                        {"key": _CHAT_USAGE_KEY, "value": {"stringValue": json.dumps(usage)}}
                    )
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

# Claude tags each LLM call with a query_source (e.g. "sdk", "agent:custom",
# "generate_session_title") in the /v1/logs api_request events, joinable to
# spans by request_id. It's not on the spans, so surface it so the origin of a
# trace/span is visible in MLflow (e.g. the standalone title-generation call).
_QUERY_SOURCE_KEY = "query_source"
_REQUEST_ID_KEY = "request_id"


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
                    input_weights = [_attr_int(by_key.get(k)) or 0 for k in _WEIGHT_INPUT_KEYS]
                    out_w = _attr_int(by_key.get(_OUTPUT_TOKEN_KEY)) or 0
                    if any(w < 0 for w in (*input_weights, out_w)):
                        continue
                    in_w = sum(input_weights)
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


def _query_source_by_request(log_records):
    """Map request_id -> query_source from the /v1/logs api_request events."""
    mapping = {}
    for rec in log_records:
        payload = rec.get("payload") or {}
        for rl in payload.get("resourceLogs", []):
            for sl in rl.get("scopeLogs", []):
                for lr in sl.get("logRecords", []):
                    attrs = {
                        a.get("key"): a.get("value")
                        for a in lr.get("attributes", [])
                        if isinstance(a, dict)
                    }
                    rid = _value_str(attrs.get(_REQUEST_ID_KEY))
                    source = _value_str(attrs.get(_QUERY_SOURCE_KEY))
                    if rid and source:
                        mapping[rid] = source
    return mapping


def _add_query_source(payloads, source_by_request):
    """Tag each LLM span with its query_source, joined from the logs by
    request_id, so the call's origin (e.g. generate_session_title) is visible in
    MLflow. Existing query_source attributes are left untouched."""
    if not source_by_request:
        return
    for payload in payloads:
        for rs in payload.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    attrs = span.get("attributes")
                    if not isinstance(attrs, list):
                        continue
                    by_key = {a.get("key"): a.get("value") for a in attrs if isinstance(a, dict)}
                    if _QUERY_SOURCE_KEY in by_key:
                        continue
                    rid = _value_str(by_key.get(_REQUEST_ID_KEY))
                    source = source_by_request.get(rid) if rid else None
                    if source:
                        attrs.append({"key": _QUERY_SOURCE_KEY, "value": {"stringValue": source}})


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
    payload = _close_incomplete_spans(_add_token_usage(_fixup_ids(copy.deepcopy(payload))))
    request = ExportTraceServiceRequest()
    json_format.ParseDict(payload, request)
    return request.SerializeToString()


def _extract_trace_ids(payloads):
    """Extract unique trace IDs from OTLP trace payloads.

    Returns IDs in MLflow's ``tr-<hex>`` format so they are directly
    grep-able against MLflow UI URLs and dashboard links.
    """
    trace_ids = set()
    for payload in payloads:
        for rs in payload.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    tid = span.get("traceId")
                    if tid:
                        trace_ids.add(f"tr-{tid}")
    return sorted(trace_ids)


_METRIC_KINDS = ("sum", "gauge", "histogram", "exponentialHistogram", "summary")


def _extract_session_ids(payloads, metric_records):
    """Extract unique session IDs from span attributes and metric records."""
    session_ids = set()
    for payload in payloads:
        for rs in payload.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    attrs = span.get("attributes")
                    if not isinstance(attrs, list):
                        continue
                    for a in attrs:
                        if isinstance(a, dict) and a.get("key") == _SESSION_ID_KEY:
                            sid = _value_str(a.get("value"))
                            if sid:
                                session_ids.add(sid)
    for rec in metric_records:
        payload = rec.get("payload") or {}
        for rm in payload.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for metric in sm.get("metrics", []):
                    for kind in _METRIC_KINDS:
                        for dp in metric.get(kind, {}).get("dataPoints", []):
                            for a in dp.get("attributes", []):
                                if isinstance(a, dict) and a.get("key") == _SESSION_ID_KEY:
                                    sid = _value_str(a.get("value"))
                                    if sid:
                                        session_ids.add(sid)
    return sorted(session_ids)


class PushResult(NamedTuple):
    ok: int
    err: int
    trace_ids: list[str]
    session_ids: list[str]


def push_traces(log_file, endpoint, experiment, token=None):
    """Push /v1/traces records from a JSONL log to an MLflow OTLP endpoint."""
    endpoint = endpoint.rstrip("/")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    trace_records = []
    metric_records = []
    log_records = []
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
                elif "/v1/logs" in path:
                    log_records.append(rec)
    except FileNotFoundError:
        return PushResult(0, 0, [], [])

    if not trace_records:
        return PushResult(0, 0, [], [])

    experiment_id = _resolve_experiment_id(endpoint, experiment, headers)
    if not experiment_id:
        print(
            f"MLflow experiment '{experiment}' not found — skipping trace push.",
            file=sys.stderr,
        )
        return PushResult(0, 0, [], [])

    payloads = [rec["payload"] for rec in trace_records if rec.get("payload")]

    trace_ids = _extract_trace_ids(payloads)
    session_ids = _extract_session_ids(payloads, metric_records)

    # Annotate spans (from the metrics/logs streams) before serialization.
    _add_span_costs(payloads, _cost_by_session(metric_records))
    _add_query_source(payloads, _query_source_by_request(log_records))

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

    return PushResult(ok, err, trace_ids, session_ids)
