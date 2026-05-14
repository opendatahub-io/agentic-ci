---
name: test-e2e-podman
description: Run end-to-end tests for the podman backend using a real container and Claude API call
---

# End-to-End Podman Backend Test

Run a full lifecycle test of the podman backend: setup, run (multiple modes), and stop.

## Prerequisites

- `podman` installed and working (rootless)
- GCP ADC credentials available (`~/.config/gcloud/application_default_credentials.json`)
- Network access to Vertex AI and `ghcr.io`

## Steps

Clean up any leftover container first:

```bash
podman rm -f agentic-ci 2>/dev/null || true
```

### 1. Setup

```bash
uv run agentic-ci setup --image ghcr.io/opendatahub-io/ai-helpers:latest
```

Verify:
- Output says `--- Podman container started ---`
- `podman ps --filter name=agentic-ci` shows the container running

### 2. First run (streaming, no OTEL)

```bash
uv run agentic-ci run "Respond with exactly: RUN1_OK" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest \
    --model claude-sonnet-4-6 --no-otel
```

Verify:
- Output says `--- Podman container already running ---` (reuses container from setup)
- Streaming output shows colored text blocks (thinking, tool calls, Claude response)
- Claude's response contains `RUN1_OK`
- Exit code is 0

### 3. Second run (streaming, with OTEL)

```bash
uv run agentic-ci run "Respond with exactly: RUN2_OK" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest \
    --model claude-sonnet-4-6
```

Verify:
- Container is still reused (`already running`)
- OTEL collector starts on a dynamic port
- Streaming output works
- Claude's response contains `RUN2_OK`
- OTEL Token/Cost Summary prints at the end with token counts and USD costs
- Exit code is 0

### 4. Run without streaming

```bash
uv run agentic-ci run "Respond with exactly: RUN3_OK" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest \
    --model claude-sonnet-4-6 --no-streaming --no-otel
```

Verify:
- No formatted streaming output (no colored blocks, no tool summaries)
- Exit code is 0

### 5. Stop

```bash
uv run agentic-ci stop --image ghcr.io/opendatahub-io/ai-helpers:latest
```

Verify:
- Output says `--- Podman container stopped ---`
- `podman ps -a --filter name=agentic-ci` shows no container (removed)

## Running the full suite

Execute all steps sequentially. If any step fails, stop and investigate. Clean up with `podman rm -f agentic-ci` before retrying.
