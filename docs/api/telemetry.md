# Generic Event Transport

`agentic_ci.telemetry` transports producer-owned event mappings through the
existing OTLP trace path used by the MLflow exporter. It does not define lifecycle stages,
outcomes, schemas, issue trackers, or workflow orchestration.

## Event boundary

An event must provide a non-empty `event_type` field. Producers can pass a
JSON-compatible mapping or an immutable object implementing `to_dict()`.
Additional safe fields are preserved unchanged and serialized in the OTLP
span event body.

The transport rejects content rather than silently redacting and changing a
producer schema. Sensitive field names, email addresses, bearer credentials,
private-key material, URL userinfo, and secret-bearing query parameters are
not accepted in event or correlation values. Prompt, description, comment,
diff, source-code, request/response body, username, and credential fields must
be reduced to approved identifiers or aggregate metrics by the producer.
Events are also bounded by nesting, collection, string, and serialized-size
limits. Token-count fields such as `input_tokens` remain valid.

Correlation identifiers are supplied separately as an arbitrary mapping. The
transport emits them as `telemetry.correlation.<name>` attributes. A valid
32-character hexadecimal `trace_id` joins the event to an existing trace;
otherwise the transport creates a trace ID from the event identity. Existing
root spans in a JSONL log are used as the event span's parent.

## Emit or export

```python
from agentic_ci.telemetry import emit_event

emit_event(
    {
        "event_type": "workflow.completed",
        "event_id": "run-42-complete",
        "result": "accepted",
    },
    log_root="_run",
    correlation={
        "work_item": "item-42",
        "pipeline": "pipeline-7",
        "job": "job-12",
        "trace_id": "0123456789abcdef0123456789abcdef",
    },
)
```

File emission always appends to `claude-otel.jsonl` beneath the runner-owned
`log_root`. Symbolic-link roots and symbolic-link log files are rejected, and
the file is opened without following links. Producers cannot select an
arbitrary output path.

Use a trusted `endpoint="http://collector:4318"` to POST the same OTLP trace
payload to an HTTP collector, or provide both destinations. Endpoint URLs and
authorization headers are runner configuration, not producer event input.
`emit_event()` and `export_event()` raise transport errors. The caller decides
whether an export failure is fatal, so event transport cannot silently change
a workflow result.

::: agentic_ci.telemetry
