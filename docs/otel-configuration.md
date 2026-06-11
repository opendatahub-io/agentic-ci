# OTel Configuration by Harness

Each agent harness configures OpenTelemetry differently based on the
agent CLI's telemetry implementation. The local OTLP collector accepts
all signals (metrics, logs, traces) regardless of harness — the
differences are in what the agent exports and what env vars control it.

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
| `OTEL_METRIC_EXPORT_INTERVAL` | `10000` | Export metrics every 10s. Controls Claude Code's OTel metrics SDK export frequency. |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | `1` | Rich span hierarchy with tool nesting and subagent spans |
| `OTEL_LOG_USER_PROMPTS` | `1` | Include user prompt text in spans |
| `OTEL_LOG_TOOL_DETAILS` | `1` | Include tool input parameters in spans |
| `OTEL_LOG_TOOL_CONTENT` | `1` | Include tool output/results in spans |

**Not set:** `OTEL_BSP_SCHEDULE_DELAY` — Claude Code manages its own
span flush lifecycle, so the default batch processor delay is fine.

## OpenCode

OpenCode uses the Vercel AI SDK's OpenTelemetry integration. OTel must
be enabled in `opencode.json` (`experimental.openTelemetry: true`) in
addition to the env vars — the harness writes this config automatically
via `write_sandbox_config()`.

| Variable | Value | Purpose |
|----------|-------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://...:{port}` | Collector address |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/json` | OTLP wire format |
| `OTEL_BSP_SCHEDULE_DELAY` | `0` | Flush spans immediately. Required because OpenCode calls `process.exit()` which kills the Node.js process before the OTel batch span processor can drain its queue. Without this, spans are lost. |

**Not set:**

- `CLAUDE_CODE_ENABLE_TELEMETRY` — Claude-specific, not used by OpenCode
- `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` — OpenCode doesn't
  export metrics or logs via OTel. Cost and token data come from JSON
  stdout events (`step_finish` type), not from the OTel pipeline.
- `OTEL_METRIC_EXPORT_INTERVAL` — no OTel metrics to export
- `OTEL_LOG_USER_PROMPTS` / `OTEL_LOG_TOOL_*` — Claude-specific flags

## Sandbox config

The `opencode.json` config is written by `write_sandbox_config()` and
mounted into the container by the backend:

- **Podman**: mounted as a read-only volume via `sandbox_config_mounts()`
- **OpenShell**: uploaded and moved into place via `_upload_sandbox_config()`

Claude Code does not require sandbox config for OTel — env vars are
sufficient.
