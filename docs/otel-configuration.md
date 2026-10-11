# OTel Configuration by Harness

Each agent harness configures OpenTelemetry differently based on the
agent CLI's telemetry implementation. The local OTLP collector accepts
all signals (metrics, logs, traces) regardless of harness -- the
differences are in what the agent exports and what env vars control it.

For the full telemetry architecture (collector, trace completeness,
MLflow pipeline), see [OTEL Architecture](otel-architecture.md).

## Claude Code

Claude Code has its own telemetry system that exports via the standard
OTLP protocol.

| Variable | Value | Purpose |
|----------|-------|---------|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` | Master switch for telemetry export |
| `OTEL_METRICS_EXPORTER` | `otlp` | Export metrics (token counts, cost) |
| `OTEL_LOGS_EXPORTER` | `otlp` | Export log events (API requests, tool decisions) |
| `OTEL_TRACES_EXPORTER` | `otlp` | Export trace spans (tool calls, LLM requests) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/json` | OTLP wire format |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://...:{port}` | Collector address |
| `OTEL_BSP_SCHEDULE_DELAY` | `1000` | Flush spans every 1s. Reduces span loss on container crash vs the default 5s. |
| `OTEL_METRIC_EXPORT_INTERVAL` | `10000` | Export metrics every 10s. Controls Claude Code's OTel metrics SDK export frequency. |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | `1` | Rich span hierarchy with tool nesting and subagent spans |
| `OTEL_LOG_USER_PROMPTS` | `1` | Include user prompt text in spans |
| `OTEL_LOG_TOOL_DETAILS` | `1` | Include tool input parameters in spans |
| `OTEL_LOG_TOOL_CONTENT` | `1` | Include tool output/results in spans |
| `TRACEPARENT` | `00-{trace_id}-{span_id}-01` | W3C Trace Context. If the agent's OTEL SDK respects it, spans parent under the orchestrator's root span. |

## OpenCode

OpenCode exports OTLP spans via the Vercel AI SDK's OpenTelemetry
integration. On the local backend the harness wires the collector endpoint
through `build_local_env()`; on Podman and OpenShell the same variables
flow through `build_otel_exec_env()` / `build_env_script_lines()`.

| Variable | Value | Purpose |
|----------|-------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://...:{port}` | Collector address |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/json` | OTLP wire format (the collector parses JSON bodies) |
| `OTEL_BSP_SCHEDULE_DELAY` | `0` | Flush spans immediately. Required because OpenCode calls `process.exit()` which kills the Node.js process before the OTel batch span processor can drain its queue. Without this, spans are lost. |
| `TRACEPARENT` | `00-{trace_id}-{span_id}-01` | W3C Trace Context. Same as Claude Code. |

**Telemetry enablement is caller-owned on the local backend.** For a local
run agentic-ci never writes OpenCode config: OpenCode v2.0.26 exports OTLP
as soon as the endpoint env vars are set (its `experimental.openTelemetry`
config key is reported as an "omitted unsupported legacy setting"); older
versions additionally require `experimental.openTelemetry: true` in the
caller-owned config (`OPENCODE_CONFIG_DIR`). The container backends
still seed that key for those older versions by writing `opencode.json`
via `write_sandbox_config()` (see [Sandbox config](#sandbox-config)).
Note that `opencode run` delegates to a background
service when one is already running — a service started without the OTEL
env vars exports nothing for its LLM work, so spans are complete only when
the run starts its own service (a fresh machine or container, or
`--standalone`).

**Token/cost metrics do not come from OTEL.** Verified against a live
OpenCode v2.0.26 run: the exported spans (CLI lifecycle, session runner,
LLM transport, tool execution) and log records carry no token or cost
usage — only HTTP rate-limit headers mention tokens. On the local backend
agentic-ci therefore persists the usage that `OpenCodeStreamProcessor`
parses from the `step_finish` NDJSON events into the run's OTEL JSONL
artifact (`opencode_usage_records()` in `agentic_ci/otel.py`), as OTLP
records shaped like Claude Code's metrics, so `parse_metrics`, the
token/cost summary and the run-result consumers work uniformly across
harnesses. The container backends do not persist stream usage yet: their
OpenCode runs carry spans but no token/cost rows.
Two caveats, both inherent to the OpenCode stream:

- OpenCode emits `step_finish` only for steps that end in tool calls, so
  the final text-only step of a run is not counted — the persisted usage
  is a lower bound.
- Cost is included only when OpenCode reports it (it does on v2.0.26);
  nothing is estimated and no price table is embedded.

**Not set:**

- `CLAUDE_CODE_ENABLE_TELEMETRY` — Claude-specific, not used by OpenCode
- `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` — OpenCode exports spans
  through its own integration; usage metrics are persisted by agentic-ci
  from the stream instead (see above)
- `OTEL_METRIC_EXPORT_INTERVAL` — no OTel metrics to export
- `OTEL_LOG_USER_PROMPTS` / `OTEL_LOG_TOOL_*` — Claude-specific flags

### Artifact naming

The collector writes one JSONL artifact per run, named after the harness:

| Harness | OTEL JSONL artifact |
|---------|---------------------|
| Claude Code | `claude-otel.jsonl` (unchanged, historical name) |
| OpenCode | `opencode-otel.jsonl` |
| Codex | `claude-otel.jsonl` (historical name kept for existing consumers) |

The name comes from `otel_log_filename(harness)` in `agentic_ci/otel.py`;
the rate file sits next to it (`opencode-otel-rate.json`, say). Telemetry
events (`skill.routed`) join the same artifact via the `log_filename`
parameter of `emit_event`.

## Codex

Codex configures OTel through its `[otel]` configuration rather than standard
OTel environment variables. For each run, the Codex harness passes `-c`
overrides for three OTLP/HTTP JSON exporters:

| Signal | Endpoint |
|--------|----------|
| Logs | `http://...:{port}/v1/logs` |
| Metrics | `http://...:{port}/v1/metrics` |
| Traces | `http://...:{port}/v1/traces` |

| Variable | Value | Purpose |
|----------|-------|---------|
| `TRACEPARENT` | `00-{trace_id}-{span_id}-01` | W3C Trace Context. Codex parents its `exec` root span under it, so its spans join the agentic-ci root trace. Spans Codex spawns without context propagation (`auth`, `code_mode.broker.invoke_tool`) still start their own traces. |

The log stream includes API requests, response events with token counts, tool
decisions/results, and redacted prompt metadata. User prompt content remains
disabled by default. The metrics stream includes Codex API, streaming, and tool
counters and duration histograms. Exporters flush when Codex shuts down.

Codex reports token usage rather than dollar cost. agentic-ci estimates USD
cost from a generated snapshot of LiteLLM's OpenAI model-price map, including
cache and `priority`/`flex` service-tier rates. `make update-cost-map` resolves
LiteLLM `main` to an immutable commit SHA, fetches the map from that revision,
and records the SHA alongside the generated pricing data. Set
`AGENTIC_CI_LITELLM_COST_MAP` to a path containing a LiteLLM-format JSON map to
override the bundled snapshot. Token usage is still shown when a model has no
known price; the cost table omits that model.

The endpoint host is backend-specific: loopback for local and host-networked
Podman runs, and `host.openshell.internal` for OpenShell.

## Sandbox config

The `opencode.json` config is written by `write_sandbox_config()` and
mounted into the container by the backend:

- **Podman**: mounted as a read-only volume via `sandbox_config_mounts()`
- **OpenShell**: uploaded and moved into place via `_upload_sandbox_config()`

Claude Code and Codex do not require a mounted sandbox config for OTel.
Claude Code uses environment variables; Codex receives per-run CLI config
overrides.
