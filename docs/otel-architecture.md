# OTEL Telemetry Architecture

End-to-end architecture for capturing, storing, and exporting OpenTelemetry
data from agent runs. Covers the collector, trace completeness guarantees,
and the MLflow export pipeline.

## Data flow overview

```text
                  Container / Sandbox / Local
                 ┌─────────────────────────────┐
                 │  Agent (Claude Code / OC)    │
                 │  ┌───────────────────────┐   │
                 │  │ OTEL SDK              │   │
                 │  │  - traces (spans)     │   │
                 │  │  - metrics (tokens)   │   │
                 │  │  - logs (api events)  │   │
                 │  └───────┬───────────────┘   │
                 └──────────┼───────────────────┘
                            │ OTLP HTTP/JSON
                            v
              Host ┌─────────────────────────┐
                   │  otel.py collector      │
                   │  (stdlib HTTPServer)    │
                   │                         │
                   │  POST /v1/traces   ──┐  │
                   │  POST /v1/metrics  ──┼──┼──> claude-otel.jsonl
                   │  POST /v1/logs     ──┘  │
                   │                         │
                   │  Token rate tracker ─────┼──> claude-otel-rate.json
                   └─────────────────────────┘
                            │
                            v
              ┌──────────────────────────────┐
              │  cli.py orchestrator         │
              │                              │
              │  1. Stop collector           │
              │  2. Inject synthetic root    │
              │     spans (if needed)        │
              │  3. Print token/cost summary │
              │  4. Copy JSONL to artifacts  │
              └──────────┬───────────────────┘
                         │
                         v
              ┌──────────────────────────────┐
              │  agentic-ci mlflow-push      │
              │  (separate CI job)           │
              │                              │
              │  1. Read JSONL               │
              │  2. Enrich spans:            │
              │     - gen_ai.usage.*         │
              │     - mlflow.chat.tokenUsage │
              │     - mlflow.llm.cost        │
              │     - query_source           │
              │  3. Push to MLflow /v1/traces│
              │  4. Finalize stuck traces    │
              └──────────────────────────────┘
```

## Collector (otel.py)

The collector is a lightweight OTLP HTTP/JSON receiver built on Python's
`http.server.HTTPServer`. It runs on the host, outside the container or
sandbox.

**Why a custom collector instead of the standard OpenTelemetry Collector?**
The OTEL Collector is a 50+ MB Go binary with complex configuration. We
need only three things: accept OTLP JSON, write to a file, and track
token rates. The stdlib HTTP server does this in ~300 lines with zero
additional dependencies.

### Lifecycle

1. `start_collector(run_dir)` spawns the collector as a subprocess on a
   dynamic port. The port is written to a file so the CLI can read it.
2. The agent runs and exports telemetry to `http://host:{port}`.
3. After the agent exits, `stop_collector(proc)` sends SIGTERM (with a
   SIGKILL fallback after 5s).

### Endpoints

| Path | Method | Purpose |
|------|--------|---------|
| `/v1/traces` | POST | Accept OTLP trace spans |
| `/v1/metrics` | POST | Accept OTLP metrics (token counts, cost) |
| `/v1/logs` | POST | Accept OTLP log events (API requests) |

All payloads are appended as JSON lines to the OTEL log file
(`claude-otel.jsonl`). Each line is a JSON object with `ts`, `path`,
and `payload` fields.

### Token rate tracking

On each `/v1/metrics` POST, the collector extracts
`claude_code.token.usage` data points and maintains a 60-second sliding
window. The current total and rate (tokens/second) are written atomically
to a rate file (`claude-otel-rate.json`). External tooling can read this
file during the run for live monitoring.

## Trace completeness

Agent runs can end in several ways, and each has different implications
for trace data:

| Exit type | Agent root span? | Child spans? | Action needed |
|-----------|-------------------|--------------|---------------|
| Normal exit | Yes | Yes | None, trace is complete |
| Graceful shutdown (SIGTERM) | Usually | Yes | Flush wait covers it |
| Container crash (SIGKILL) | No | Partial | Synthetic root needed |
| OOM kill | No | Partial | Synthetic root needed |
| CI timeout | No | Partial | Synthetic root needed |
| Immediate crash (init failure) | No | No | Fallback root needed |

### Orchestrator-owned root span

The orchestrator (`cli.py`) guarantees trace completeness by injecting
synthetic root spans into the JSONL after the agent exits. This happens
in the `finally` block of `cmd_run()`, so it runs on normal exit,
container crash, and KeyboardInterrupt alike.

**Before the run:**

```python
trace_id, span_id, traceparent = otel.generate_trace_context()
start_ns = time.time_ns()
```

The orchestrator generates a W3C Trace Context and records the start
timestamp. The `TRACEPARENT` env var is passed into the agent environment.
If the agent's OTEL SDK respects it, all agent spans become children of
the orchestrator's root span.

**After the run (in `finally`):**

```python
otel.inject_root_spans(
    otel_log,
    start_ns,
    end_ns,
    rc,
    fallback_trace_id=trace_id,
    fallback_span_id=span_id,
    attributes={...},
)
```

`inject_root_spans()` scans the JSONL for traces that have child spans
but no root span (a span with no `parentSpanId`). For each orphan trace,
it appends a synthetic root span with:

- The orphan trace's own trace ID (so it joins the existing spans)
- The orchestrator's span ID (so TRACEPARENT-parented children connect)
- Timing from `min(orchestrator_start, child_start)` to
  `max(orchestrator_end, child_end)`
- `status.code = OK` (exit 0) or `ERROR` (non-zero exit)
- `agentic_ci.synthetic_root = true` attribute for identification
- Agent metadata: backend, harness, model

If the agent emitted zero spans (total crash before OTEL init), a
fallback root span is created using the orchestrator's pre-generated
trace ID. MLflow always has at least one complete trace.

### TRACEPARENT propagation

The `TRACEPARENT` env var follows the
[W3C Trace Context](https://www.w3.org/TR/trace-context/) format:

```
00-{trace_id}-{span_id}-01
```

When the agent's OTEL SDK picks this up, all agent spans share the
orchestrator's trace ID and parent under its span ID. The synthetic root
span reuses this span ID, creating a clean parent-child hierarchy:

```text
agentic-ci-run (synthetic root, span_id=orchestrator)
  └── claude_code.session (agent root, parentSpanId=orchestrator)
        ├── claude_code.llm_request
        ├── claude_code.tool
        └── ...
```

If the agent does not respect `TRACEPARENT` (e.g. OpenCode, which
creates its own trace ID), the synthetic root is injected into the
agent's trace using the agent's trace ID. The scanner detects the
most common dangling `parentSpanId` among orphan children and reuses
it as the synthetic root's `spanId`, reconnecting the span tree:

```text
agentic-ci-run (synthetic root, spanId = dangling parentSpanId)
  ├── opencode.llm_request (parentSpanId matches synthetic root)
  ├── opencode.tool (parentSpanId matches synthetic root)
  └── ...
```

If the agent's root span was flushed (no orphan), no injection occurs.

### BSP schedule delay

The Batch Span Processor (BSP) in the OTEL SDK buffers spans and flushes
them periodically. The default interval is 5 seconds, which means spans
can be lost if the process exits before the next flush.

| Harness | `OTEL_BSP_SCHEDULE_DELAY` | Reason |
|---------|---------------------------|--------|
| Claude Code | `1000` (1s) | Faster flush reduces span loss on crash |
| OpenCode | `0` (immediate) | `process.exit()` kills Node.js before any batch flush |

After the agent process exits, `_wait_for_otel_flush()` sleeps 7 seconds
to let in-flight HTTP requests from the agent's OTEL exporter drain to
the collector. This is a belt-and-suspenders measure: the synthetic root
span guarantees trace completeness regardless of whether child spans
arrive.

## OTEL-to-MLflow pipeline

The JSONL file is the intermediate format between the agent run and
MLflow. The `agentic-ci mlflow-push` command reads this file and pushes
enriched traces to MLflow's OTLP endpoint.

### Span enrichment

Before pushing, `mlflow-push` enriches spans with attributes that MLflow
needs but the agent does not natively emit:

#### Token usage

Claude Code emits bare `input_tokens`, `output_tokens`,
`cache_read_tokens`, and `cache_creation_tokens` as span attributes.
These are translated into two formats:

- **OTEL GenAI standard** (`gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`): For non-MLflow backends that read the
  convention. No cache field exists in the standard.

- **MLflow native** (`mlflow.chat.tokenUsage`): A JSON attribute with
  `input_tokens`, `output_tokens`, `total_tokens`,
  `cache_read_input_tokens`, and `cache_creation_input_tokens`. MLflow
  aggregates these into `mlflow.trace.tokenUsage` for the experiment
  Usage dashboard. All four cache lines appear on the dashboard.

Note: `input_tokens` in Claude's schema means *fresh* (non-cached) input
tokens. This is disjoint from `cache_read_tokens` and
`cache_creation_tokens`. MLflow's auto-cost calculator assumes
`prompt_tokens` includes cached tokens and can go negative when fed
Claude's disjoint counts, which is why we set `mlflow.llm.cost`
explicitly instead.

#### Cost attribution

Claude reports exact spend as a delta-temporality OTEL metric
(`claude_code.cost.usage`) tagged by `session.id`. This is a
session-level total, not per-span.

`mlflow-push` distributes the session cost across LLM spans weighted by
token volume (input + output + cache). Each span gets an
`mlflow.llm.cost` attribute with `{input_cost, output_cost, total_cost}`.
MLflow aggregates these into `mlflow.trace.cost`. The per-span split is
an approximation; the session total is exact.

#### Query source

Claude tags each API call with a `query_source` (e.g., `"sdk"`,
`"agent:custom"`, `"generate_session_title"`) in `/v1/logs` events,
joinable to spans by `request_id`. `mlflow-push` copies this attribute
onto the matching span so the call origin is visible in MLflow's trace
viewer.

### Trace finalization

After pushing all payloads, `mlflow-push` batch-fetches trace status via
`POST /api/3.0/mlflow/traces/batchGetInfos`. Any trace still in
`IN_PROGRESS` state (root span was never received by MLflow) is marked
as `ERROR` via `PATCH /api/2.0/mlflow/traces/{id}`.

With synthetic root span injection, most traces arrive complete and
finalization is a safety net. It catches edge cases like network drops
between the push and MLflow ingestion.

### JSONL record format

Each line in the JSONL file is a JSON object:

```json
{
  "ts": "2024-01-15T10:30:00.123456+00:00",
  "path": "/v1/traces",
  "payload": { ... OTLP JSON payload ... }
}
```

The `path` field indicates the signal type:
- `/v1/traces`: Span data (trace hierarchy, tool calls, LLM requests)
- `/v1/metrics`: Cumulative metrics (token counts, cost, active time)
- `/v1/logs`: Log events (API request details, query source)

Synthetic root spans injected by the orchestrator are appended as
additional `/v1/traces` records at the end of the file. They are
indistinguishable from agent-emitted spans except for the
`agentic_ci.synthetic_root = true` attribute.

## Network topology

The collector runs on the host. How the agent reaches it depends on the
backend:

| Backend | Agent endpoint | Mechanism |
|---------|---------------|-----------|
| Local | `http://127.0.0.1:{port}` | Same host, direct |
| Podman | `http://127.0.0.1:{port}` | `--network host` |
| OpenShell | `http://host.openshell.internal:{port}` | Gateway host resolution, network policy allows the port |

For OpenShell, the sandbox network policy must explicitly allow the
OTEL port. This is handled automatically by `sandbox.create()` when
`otel_port` is provided.

The collector binds to `127.0.0.1` for Local and Podman backends, and
`0.0.0.0` for OpenShell (since the sandbox resolves the host via a
different interface).
