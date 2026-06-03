---
name: test-e2e-podman
description: Run end-to-end tests for the podman backend using real container and API calls
---

# End-to-End Podman Backend Test

Run full lifecycle tests of the podman backend across harnesses and auth modes.

Each section below is independent. Run whichever sections match your environment and skip the rest.

## Prerequisites

All sections require:
- `podman` installed and working (rootless)
- Network access to container registries (image pull)

Per-section requirements:
- **Vertex AI auth**: GCP ADC credentials (`~/.config/gcloud/application_default_credentials.json`)
- **API key auth**: `ANTHROPIC_API_KEY` set in the environment

## Container images

Default images are used unless overridden by environment variables:

| Harness    | Default image                                        | Override env var           |
|------------|------------------------------------------------------|----------------------------|
| Claude Code | `quay.io/aipcc/agentic-ci/claude-runner:latest`     | `CLAUDE_CONTAINER_IMAGE`   |
| OpenCode   | `quay.io/aipcc/agentic-ci/opencode-runner:latest`   | `OPENCODE_CONTAINER_IMAGE` |

The commands below use `$CLAUDE_IMAGE` and `$OPENCODE_IMAGE` variables. Set them at the start of the session:

```bash
CLAUDE_IMAGE="${CLAUDE_CONTAINER_IMAGE:-quay.io/aipcc/agentic-ci/claude-runner:latest}"
OPENCODE_IMAGE="${OPENCODE_CONTAINER_IMAGE:-quay.io/aipcc/agentic-ci/opencode-runner:latest}"
```

## Auth isolation

`ANTHROPIC_API_KEY` controls auth mode globally. To ensure Vertex sections use Vertex and API key sections use API key auth, prefix commands with the appropriate environment:

- Vertex sections: `env -u ANTHROPIC_API_KEY uv run --with . agentic-ci ...`
- API key sections: run normally (key inherited from environment)

The commands below already include these prefixes.

## Cleanup

Before starting any section, clean up leftover containers:

```bash
podman rm -f agentic-ci 2>/dev/null || true
```

---

## Section A: Claude Code + Vertex AI

### A1. Setup

```bash
env -u ANTHROPIC_API_KEY uv run --with . agentic-ci setup \
    --image $CLAUDE_IMAGE
```

Verify:
- Output shows `Auth: Vertex AI`
- Output says `--- Podman container started ---`
- `podman ps --filter name=agentic-ci` shows the container running

### A2. Run (streaming, no OTEL)

```bash
env -u ANTHROPIC_API_KEY uv run --with . agentic-ci run "Respond with exactly: A2_OK" \
    --image $CLAUDE_IMAGE \
    --model claude-haiku-4-5 --no-otel
```

Verify:
- Output says `--- Podman container already running ---` (reuses container from setup)
- Streaming output shows colored text blocks (thinking, tool calls, Claude response)
- Claude's response contains `A2_OK`
- Exit code is 0

### A3. Run (streaming, with OTEL)

```bash
env -u ANTHROPIC_API_KEY uv run --with . agentic-ci run "Respond with exactly: A3_OK" \
    --image $CLAUDE_IMAGE \
    --model claude-haiku-4-5
```

Verify:
- Container is still reused (`already running`)
- OTEL collector starts on a dynamic port
- Streaming output works
- Claude's response contains `A3_OK`
- OTEL Token/Cost Summary prints at the end with token counts and USD costs
- Exit code is 0

### A4. Run (no streaming)

```bash
env -u ANTHROPIC_API_KEY uv run --with . agentic-ci run "Respond with exactly: A4_OK" \
    --image $CLAUDE_IMAGE \
    --model claude-haiku-4-5 --no-streaming --no-otel
```

Verify:
- No formatted streaming output (no colored blocks, no tool summaries)
- Exit code is 0

### A5. Stop

```bash
env -u ANTHROPIC_API_KEY uv run --with . agentic-ci stop \
    --image $CLAUDE_IMAGE
```

Verify:
- Output says `--- Podman container stopped ---`
- `podman ps -a --filter name=agentic-ci` shows no container

---

## Section B: Claude Code + API Key

Requires `ANTHROPIC_API_KEY` set in the environment.

### B1. Setup and run

```bash
podman rm -f agentic-ci 2>/dev/null || true
uv run --with . agentic-ci run "Respond with exactly: B1_OK" \
    --image $CLAUDE_IMAGE \
    --model claude-haiku-4-5 --no-otel
```

Verify:
- Output shows `Auth: API key` (not `Vertex AI`)
- Streaming output works
- Claude's response contains `B1_OK`
- Exit code is 0

### B2. Run (streaming, with OTEL)

```bash
uv run --with . agentic-ci run "Respond with exactly: B2_OK" \
    --image $CLAUDE_IMAGE \
    --model claude-haiku-4-5
```

Verify:
- OTEL collector starts and Token/Cost Summary prints at the end
- Claude's response contains `B2_OK`
- Exit code is 0

### B3. Stop

```bash
uv run --with . agentic-ci stop --image $CLAUDE_IMAGE
```

Verify:
- Output says `--- Podman container stopped ---`

---

## Section C: OpenCode + Vertex AI

Requires GCP ADC credentials.

### C1. Run

```bash
podman rm -f agentic-ci 2>/dev/null || true
env -u ANTHROPIC_API_KEY uv run --with . agentic-ci run "Respond with exactly: C1_OK" \
    --harness opencode \
    --image "$OPENCODE_IMAGE" \
    --model google-vertex/claude-haiku-4-5@20251001 \
    --no-otel
```

Verify:
- Output shows `Harness: OpenCode` and `Auth: Vertex AI`
- Streaming output shows agent activity
- Exit code is 0

### C2. Stop

```bash
env -u ANTHROPIC_API_KEY uv run --with . agentic-ci stop --harness opencode \
    --image "$OPENCODE_IMAGE"
```

Verify:
- Output says `--- Podman container stopped ---`

---

## Section D: OpenCode + API Key

Requires `ANTHROPIC_API_KEY` set in the environment.

### D1. Run

```bash
podman rm -f agentic-ci 2>/dev/null || true
uv run --with . agentic-ci run "Respond with exactly: D1_OK" \
    --harness opencode \
    --image "$OPENCODE_IMAGE" \
    --model anthropic/claude-haiku-4-5-20251001 \
    --no-otel
```

Verify:
- Output shows `Harness: OpenCode` and `Auth: API key`
- Streaming output shows agent activity
- Exit code is 0

### D2. Stop

```bash
uv run --with . agentic-ci stop --harness opencode \
    --image "$OPENCODE_IMAGE"
```

Verify:
- Output says `--- Podman container stopped ---`

---

## Running the full suite

Execute sections in order (A through D), skipping any whose prerequisites are not met. If any step fails, stop and investigate. Clean up with `podman rm -f agentic-ci` before retrying.
