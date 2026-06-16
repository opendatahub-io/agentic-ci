---
name: test-e2e-podman
description: Run end-to-end tests for the podman backend using real container and API calls
---

# End-to-End Podman Backend Test

Run full lifecycle tests of the podman backend across harnesses and auth modes.
All tests run inside a CI container with nested podman.

Each section below is independent. Run whichever sections match your environment and skip the rest.

## Before you start

Present the following default container images to the user and ask them
to confirm or override each one before proceeding:

| Role             | Default image                                          | Variable                  |
|------------------|--------------------------------------------------------|---------------------------|
| CI image         | `quay.io/aipcc/agentic-ci/podman`                     | `$CI_IMAGE`               |
| Claude runner    | `quay.io/aipcc/agentic-ci/claude-runner:latest`        | `$CLAUDE_IMAGE`           |
| OpenCode runner  | `quay.io/aipcc/agentic-ci/opencode-runner:latest`      | `$OPENCODE_IMAGE`         |

Also ask for:

- **API key file** — path to a local file containing an Anthropic API key
  (one line, no whitespace). Needed for Sections B and D. Store as
  `$API_KEY_FILE`.

Per-section requirements:
- **Vertex AI auth** (Sections A, E): GCP ADC credentials at
  `~/.config/gcloud/application_default_credentials.json` and
  `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION` set in the environment.
- **API key auth** (Sections B, D, F): The API key file above.

## Container setup

All agentic-ci commands run inside the CI container. The container needs
`--privileged` for nested podman (the podman backend starts a second
container inside the CI container).

### Start the test container

```bash
podman rm -f podman-e2e 2>/dev/null || true
podman run -d --name podman-e2e \
  --privileged \
  -v "$(pwd):/workspace:ro,z" \
  -v ~/.config/gcloud:/host-gcloud:ro,z \
  -v "$API_KEY_FILE:/host-api-key:ro,z" \
  $CI_IMAGE \
  sleep infinity
```

### Prepare the environment

```bash
podman exec podman-e2e bash -c '
  mkdir -p ~/.config/gcloud
  cp /host-gcloud/application_default_credentials.json ~/.config/gcloud/
  cd /workspace && uv pip install --system --no-cache .
'
```

Verify:
- `podman exec podman-e2e agentic-ci --help` prints usage
- `podman exec podman-e2e podman --version` shows podman is available

## Cleanup between tests

Reset the inner agentic-ci container between sections:

```bash
podman exec podman-e2e bash -c 'podman rm -f agentic-ci 2>/dev/null || true'
```

## Models

Use haiku to keep cost down. The model name format varies by harness:

| Harness     | Vertex AI model           | API key model                          |
|-------------|---------------------------|----------------------------------------|
| Claude Code | `claude-haiku-4-5`        | `claude-haiku-4-5`                     |
| OpenCode    | (not supported via Vertex) | `anthropic/claude-haiku-4-5-20251001` |

---

## Section A: Claude Code + Vertex AI

Requires GCP ADC credentials and `ANTHROPIC_VERTEX_PROJECT_ID`.

### A1. Setup

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci setup --image "$CLAUDE_IMAGE"
  '
```

Verify:
- Output shows `Auth: Vertex AI`
- Output says `--- Podman container started ---`
- `podman exec podman-e2e podman ps --filter name=agentic-ci` shows the
  inner container running

### A2. Run (streaming, no OTEL)

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run "Respond with exactly: A2_OK" \
      --image "$CLAUDE_IMAGE" \
      --model claude-haiku-4-5 --no-otel
  '
```

Verify:
- Output says `--- Podman container already running ---` (reuses container
  from setup)
- Streaming output shows colored text blocks (thinking, tool calls, Claude
  response)
- Claude's response contains `A2_OK`
- Exit code is 0

### A3. Run (streaming, with OTEL)

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run "Respond with exactly: A3_OK" \
      --image "$CLAUDE_IMAGE" \
      --model claude-haiku-4-5
  '
```

Verify:
- Container is still reused (`already running`)
- OTEL collector starts on a dynamic port
- Streaming output works
- Claude's response contains `A3_OK`
- OTEL Token/Cost Summary prints at the end with token counts and USD costs
- Exit code is 0

### A3a. Verify trace records for mlflow-push

Check that the OTEL JSONL log contains `/v1/traces` records — these are
what `agentic-ci mlflow-push` needs:

```bash
podman exec podman-e2e bash -c '
  JSONL=$(ls -t /tmp/agentic-ci-run.*/claude-otel.jsonl 2>/dev/null | head -1)
  echo "JSONL file: $JSONL"
  grep -c "/v1/traces" "$JSONL"
'
```

Verify:
- The JSONL file exists
- The trace count is at least 1
- Optionally inspect a trace record:
  ```bash
  podman exec podman-e2e bash -c '
    JSONL=$(ls -t /tmp/agentic-ci-run.*/claude-otel.jsonl 2>/dev/null | head -1)
    grep "/v1/traces" "$JSONL" | head -1 | python3 -m json.tool
  '
  ```
  - Record has `"path"` containing `/v1/traces`
  - Record has `"payload"` with `"resourceSpans"` array

### A4. Run (no streaming)

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run "Respond with exactly: A4_OK" \
      --image "$CLAUDE_IMAGE" \
      --model claude-haiku-4-5 --no-streaming --no-otel
  '
```

Verify:
- No formatted streaming output (no colored blocks, no tool summaries)
- Exit code is 0

### A5. Stop

```bash
podman exec \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci stop --image "$CLAUDE_IMAGE"
  '
```

Verify:
- Output says `--- Podman container stopped ---`
- `podman exec podman-e2e podman ps -a --filter name=agentic-ci` shows no
  container

---

## Section B: Claude Code + API Key

Run cleanup first.

### B1. Setup and run

```bash
podman exec \
  -e "ANTHROPIC_API_KEY=$(cat "$API_KEY_FILE")" \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run "Respond with exactly: B1_OK" \
      --image "$CLAUDE_IMAGE" \
      --model claude-haiku-4-5 --no-otel
  '
```

Verify:
- Output shows `Auth: API key` (not `Vertex AI`)
- Streaming output works
- Claude's response contains `B1_OK`
- Exit code is 0

### B2. Run (streaming, with OTEL)

```bash
podman exec \
  -e "ANTHROPIC_API_KEY=$(cat "$API_KEY_FILE")" \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run "Respond with exactly: B2_OK" \
      --image "$CLAUDE_IMAGE" \
      --model claude-haiku-4-5
  '
```

Verify:
- OTEL collector starts and Token/Cost Summary prints at the end
- Claude's response contains `B2_OK`
- Exit code is 0

### B2a. Verify trace records for mlflow-push

Same check as A3a — verify the JSONL log has `/v1/traces` records:

```bash
podman exec podman-e2e bash -c '
  JSONL=$(ls -t /tmp/agentic-ci-run.*/claude-otel.jsonl 2>/dev/null | head -1)
  echo "JSONL file: $JSONL"
  grep -c "/v1/traces" "$JSONL"
'
```

Verify:
- The JSONL file exists
- The trace count is at least 1

### B3. Stop

```bash
podman exec \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci stop --image "$CLAUDE_IMAGE"
  '
```

Verify:
- Output says `--- Podman container stopped ---`

---

## Section C: OpenCode + Vertex AI

Requires GCP ADC credentials.

Run cleanup first.

### C1. Run

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e OPENCODE_IMAGE="$OPENCODE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run "Respond with exactly: C1_OK" \
      --harness opencode \
      --image "$OPENCODE_IMAGE" \
      --model google-vertex/claude-haiku-4-5@20251001 \
      --no-otel
  '
```

Verify:
- Output shows `Harness: OpenCode` and `Auth: Vertex AI`
- Streaming output shows agent activity
- Exit code is 0

### C2. Stop

```bash
podman exec \
  -e OPENCODE_IMAGE="$OPENCODE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci stop --harness opencode --image "$OPENCODE_IMAGE"
  '
```

Verify:
- Output says `--- Podman container stopped ---`

---

## Section D: OpenCode + API Key

Requires `ANTHROPIC_API_KEY` (via API key file).

Run cleanup first.

### D1. Run

```bash
podman exec \
  -e "ANTHROPIC_API_KEY=$(cat "$API_KEY_FILE")" \
  -e OPENCODE_IMAGE="$OPENCODE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run "Respond with exactly: D1_OK" \
      --harness opencode \
      --image "$OPENCODE_IMAGE" \
      --model anthropic/claude-haiku-4-5-20251001 \
      --no-otel
  '
```

Verify:
- Output shows `Harness: OpenCode` and `Auth: API key`
- Streaming output shows agent activity
- Exit code is 0

### D2. Stop

```bash
podman exec \
  -e OPENCODE_IMAGE="$OPENCODE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci stop --harness opencode --image "$OPENCODE_IMAGE"
  '
```

Verify:
- Output says `--- Podman container stopped ---`

---

## Section E: Skill execution — Vertex AI

Verifies that a plugin skill loads and executes correctly inside the
runner container. Uses the `git-shallow-clone` skill from the
`odh-ai-helpers` plugin to clone a repo into `/tmp` and confirm the
result.

Requires GCP ADC credentials and `ANTHROPIC_VERTEX_PROJECT_ID`.

Run cleanup first.

### E1. Run

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run \
      --image "$CLAUDE_IMAGE" \
      --model claude-haiku-4-5 --no-otel \
      "Use the /odh-ai-helpers:git-shallow-clone skill to shallow-clone https://github.com/opendatahub-io/agentic-ci.git into /tmp/agentic-ci. After the clone completes, run: ls /tmp/agentic-ci and print the output, then respond with exactly: SKILL_OK"
  '
```

Verify:
- `Plugins:` line includes `odh-ai-helpers`
- Output shows `Skill` tool invocation for `odh-ai-helpers:git-shallow-clone`
- Output shows a `Bash` tool call running `git clone`
- Output shows `ls /tmp/agentic-ci` with repo contents (e.g. `src`, `pyproject.toml`, `Makefile`)
- Agent response contains `SKILL_OK`
- `Agent exit code: 0`

### E2. Stop

```bash
podman exec \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci stop --image "$CLAUDE_IMAGE"
  '
```

---

## Section F: Skill execution — API Key

Same test as Section E but with API key auth.

Run cleanup first.

### F1. Run

```bash
podman exec \
  -e "ANTHROPIC_API_KEY=$(cat "$API_KEY_FILE")" \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci run \
      --image "$CLAUDE_IMAGE" \
      --model claude-haiku-4-5 --no-otel \
      "Use the /odh-ai-helpers:git-shallow-clone skill to shallow-clone https://github.com/opendatahub-io/agentic-ci.git into /tmp/agentic-ci. After the clone completes, run: ls /tmp/agentic-ci and print the output, then respond with exactly: SKILL_OK"
  '
```

Verify:
- Output shows `Auth: API key`
- `Plugins:` line includes `odh-ai-helpers`
- Output shows `Skill` tool invocation for `odh-ai-helpers:git-shallow-clone`
- Output shows `ls /tmp/agentic-ci` with repo contents (e.g. `src`, `pyproject.toml`, `Makefile`)
- Agent response contains `SKILL_OK`
- `Agent exit code: 0`

### F2. Stop

```bash
podman exec \
  -e CLAUDE_IMAGE="$CLAUDE_IMAGE" \
  podman-e2e bash -c '
    agentic-ci stop --image "$CLAUDE_IMAGE"
  '
```

---

## Final cleanup

```bash
podman rm -f podman-e2e
```

## Running the full suite

Execute sections in order (A through F), running the cleanup step before
each section. Skip sections whose prerequisites are not met. If any step
fails, stop and investigate. Check inner container logs with:

```bash
podman exec podman-e2e podman logs agentic-ci
```
