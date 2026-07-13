# MLflow Trace Export

Push OTel traces from agent runs to an MLflow instance for observability, cost tracking, and debugging.

For the full telemetry architecture (collector, trace completeness,
span enrichment details), see [OTEL Architecture](otel-architecture.md).

## How it works

1. **During the agent run**, the local OTLP collector captures all telemetry (metrics, logs, and traces) to a JSONL file
2. **After the run**, the orchestrator injects synthetic root spans into the JSONL for any incomplete traces (container crash, OOM, timeout)
3. **In a separate CI job**, `agentic-ci mlflow-push` reads the JSONL, enriches spans with token usage and cost attributes, and pushes to MLflow's OTLP endpoint

The trace push is intentionally decoupled from the agent run -- it runs as a follow-up job with `allow_failure: true` so trace push failures never block the pipeline.

**Security note:** Traces include user prompts, tool inputs, and tool outputs (`OTEL_LOG_USER_PROMPTS=1`, `OTEL_LOG_TOOL_DETAILS=1`, `OTEL_LOG_TOOL_CONTENT=1`). Avoid including API keys, passwords, or PII in prompts when trace export is enabled -- they will be logged to the JSONL file and forwarded to MLflow.

## Trace completeness guarantees

Every agent run produces a complete trace in MLflow, regardless of how
the run ended:

- **Normal exit**: The agent flushes its root span. No intervention needed.
- **Container crash / OOM / timeout**: The orchestrator detects orphan traces (child spans with no root) in the JSONL and injects a synthetic root span with `status=ERROR` and the exit code.
- **Immediate crash (zero spans)**: A fallback root span is created using the orchestrator's pre-generated trace ID. MLflow shows a single `agentic-ci-run` span with `status=ERROR`.

Synthetic root spans carry `agentic_ci.synthetic_root=true` so they can
be identified in the MLflow trace viewer. They also include agent
metadata (`agent.backend`, `agent.harness`, `agent.model`).

## Span enrichment

Before pushing to MLflow, spans are enriched with attributes that MLflow
needs but the agent does not natively emit:

- **Token usage**: Claude's bare `input_tokens`/`output_tokens`/`cache_*` attributes are translated into `gen_ai.usage.*` (OTEL standard) and `mlflow.chat.tokenUsage` (MLflow native with cache breakdown).
- **Cost attribution**: Session-level cost from `claude_code.cost.usage` metrics is distributed across LLM spans weighted by token volume, written as `mlflow.llm.cost`.
- **Query source**: The `query_source` from `/v1/logs` API request events is joined to spans by `request_id`, making call origins visible in MLflow.

## Prerequisites

- An MLflow instance with OTLP ingestion enabled (`/v1/traces` endpoint)
- An MLflow experiment created for the agent (e.g., `rfe-autofixer`)
- A Bearer token for authentication (e.g., from a Kubernetes ServiceAccount)

### CI variables

Set these at the project or group level:

| Variable | Description |
|----------|-------------|
| `MLFLOW_TRACKING_URI` | MLflow endpoint URL (e.g., `https://mlflow.example.com`) |
| `MLFLOW_EXPERIMENT_NAME` | MLflow experiment name |
| `MLFLOW_TRACKING_TOKEN` | Bearer token for authentication |

## GitLab CI

```yaml
stages:
  - agent
  - observe

agent-run:
  stage: agent
  script:
    - agentic-ci run --harness claude-code "$PROMPT"
  after_script:
    - cp /tmp/agentic-ci-run.*/claude-otel.jsonl . 2>/dev/null || true
  artifacts:
    paths:
      - claude-otel.jsonl
    when: always
    expire_in: 30 days

trace-push:
  stage: observe
  needs: [agent-run]
  allow_failure: true
  rules:
    - if: $MLFLOW_TRACKING_URI && $MLFLOW_EXPERIMENT_NAME
  script:
    - agentic-ci mlflow-push claude-otel.jsonl
        --endpoint "$MLFLOW_TRACKING_URI"
        --experiment "$MLFLOW_EXPERIMENT_NAME"
        --token "$MLFLOW_TRACKING_TOKEN"
```

### With multiple agents

If you run several agents in parallel, each can push to its own experiment:

```yaml
.agent-template:
  stage: agent
  after_script:
    - cp /tmp/agentic-ci-run.*/claude-otel.jsonl . 2>/dev/null || true
  artifacts:
    paths:
      - claude-otel.jsonl
    when: always

rfe-autofixer:
  extends: .agent-template
  variables:
    MLFLOW_EXPERIMENT_NAME: rfe-autofixer
  script:
    - agentic-ci run --harness claude-code "$RFE_PROMPT"

rfe-assessor:
  extends: .agent-template
  variables:
    MLFLOW_EXPERIMENT_NAME: rfe-assessor
  script:
    - agentic-ci run --harness claude-code "$ASSESSOR_PROMPT"

trace-push:
  stage: observe
  needs:
    - job: rfe-autofixer
      artifacts: true
    - job: rfe-assessor
      artifacts: true
  allow_failure: true
  parallel:
    matrix:
      - EXPERIMENT: [rfe-autofixer, rfe-assessor]
  script:
    - agentic-ci mlflow-push claude-otel.jsonl
        --endpoint "$MLFLOW_TRACKING_URI"
        --experiment "$EXPERIMENT"
```

## GitHub Actions

```yaml
name: Agent Run

on: [push, workflow_dispatch]

jobs:
  agent-run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install agentic-ci
        run: pip install agentic-ci

      - name: Run agent
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: agentic-ci run --harness claude-code "$PROMPT"

      - name: Collect OTEL log
        if: always()
        run: cp /tmp/agentic-ci-run.*/claude-otel.jsonl . 2>/dev/null || true

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: otel-log
          path: claude-otel.jsonl

  trace-push:
    runs-on: ubuntu-latest
    needs: agent-run
    if: always() && needs.agent-run.result != 'cancelled'
    continue-on-error: true
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: otel-log

      - name: Install agentic-ci
        run: pip install agentic-ci

      - name: Push traces to MLflow
        env:
          MLFLOW_TRACKING_URI: ${{ vars.MLFLOW_TRACKING_URI }}
          MLFLOW_TRACKING_TOKEN: ${{ secrets.MLFLOW_TRACKING_TOKEN }}
        run: |
          agentic-ci mlflow-push claude-otel.jsonl \
            --endpoint "$MLFLOW_TRACKING_URI" \
            --experiment "${{ vars.MLFLOW_EXPERIMENT_NAME }}" \
            --token "$MLFLOW_TRACKING_TOKEN"
```

## CLI reference

```
agentic-ci mlflow-push [-h] --endpoint URL --experiment NAME [--token TOKEN] jsonl

positional arguments:
  jsonl              Path to claude-otel.jsonl file

options:
  --endpoint URL     MLflow tracking URI (env: MLFLOW_TRACKING_URI)
  --experiment NAME  MLflow experiment name (env: MLFLOW_EXPERIMENT_NAME)
  --token TOKEN      MLflow Bearer token (env: MLFLOW_TRACKING_TOKEN)
```

All options can be set via environment variables, so the CI job can be as simple as:

```bash
agentic-ci mlflow-push claude-otel.jsonl
```
