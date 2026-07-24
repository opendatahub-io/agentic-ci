---
name: test-e2e-openshell
description: Run end-to-end tests for the openshell backend using real container and API calls
---

# End-to-End OpenShell Backend Test

Run full lifecycle tests of the OpenShell backend across harnesses and auth modes.
Claude/OpenCode are fully covered by sections A-H. Cursor is covered by the
optional Cursor block in `tests/e2e/e2e-openshell-sandbox.sh` when both
`CURSOR_SANDBOX_IMAGE` and `CURSOR_API_KEY` are set.
All tests run inside a privileged container.

Each section below is independent. Run whichever sections match your environment and skip the rest.

## Before you start

Present the following default container images to the user and ask them
to confirm or override each one before proceeding:

| Role               | Default image                                          | Variable                    |
|--------------------|--------------------------------------------------------|-----------------------------|
| CI image           | `quay.io/aipcc/agentic-ci/openshell`                  | `$CI_IMAGE`                 |
| Supervisor         | `quay.io/aipcc/agentic-ci/openshell-supervisor`        | `$SUPERVISOR_IMAGE`         |
| Claude sandbox     | `quay.io/aipcc/agentic-ci/claude-sandbox`              | `$CLAUDE_SANDBOX_IMAGE`     |
| OpenCode sandbox   | `quay.io/aipcc/agentic-ci/opencode-sandbox`            | `$OPENCODE_SANDBOX_IMAGE`   |
| Cursor sandbox     | `quay.io/aipcc/agentic-ci/cursor-sandbox`              | `$CURSOR_SANDBOX_IMAGE`     |

Also ask for:

- **API key file** — path to a local file containing an Anthropic API key
  (one line, no whitespace). Needed for Sections B and D. Store as
  `$API_KEY_FILE`.

Per-section requirements:
- **Vertex AI auth** (Sections A, C, E, F, G): GCP ADC credentials at
  `~/.config/gcloud/application_default_credentials.json` and
  `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION` set in the environment.
- **API key auth** (Sections B, D, H): The API key file above.

## Container setup

OpenShell requires `--privileged` for nested podman (user namespace mapping,
overlay mounts, network namespace creation). All agentic-ci commands run
inside this outer container.

### Start the test container

```bash
podman rm -f openshell-e2e 2>/dev/null || true
podman run -d --name openshell-e2e \
  --privileged \
  -v "$(pwd):/workspace:ro,z" \
  -v ~/.config/gcloud:/host-gcloud:ro,z \
  -v "$API_KEY_FILE:/host-api-key:ro,z" \
  $CI_IMAGE \
  sleep infinity
```

### Prepare the environment

```bash
podman exec openshell-e2e bash -c '
  mkdir -p ~/.config/gcloud
  cp /host-gcloud/application_default_credentials.json ~/.config/gcloud/
  cd /workspace && uv pip install --system --no-cache .
'
```

If the sandbox images are on a registry, the container's podman will pull
them automatically during sandbox creation. If they are only available
locally (not pushed to any registry), copy them into the container's
podman storage:

```bash
podman save $CLAUDE_SANDBOX_IMAGE -o /tmp/openshell-claude.tar
podman save $OPENCODE_SANDBOX_IMAGE -o /tmp/openshell-opencode.tar
podman cp /tmp/openshell-claude.tar openshell-e2e:/tmp/
podman cp /tmp/openshell-opencode.tar openshell-e2e:/tmp/
podman exec openshell-e2e bash -c '
  podman load -i /tmp/openshell-claude.tar
  podman load -i /tmp/openshell-opencode.tar
  rm -f /tmp/openshell-*.tar
'
rm -f /tmp/openshell-claude.tar /tmp/openshell-opencode.tar
```

Verify:
- `podman exec openshell-e2e openshell --version` shows version output
- `podman exec openshell-e2e agentic-ci --help` prints usage
- `podman exec openshell-e2e podman images` lists both sandbox images

## Cleanup between tests

The gateway and sandbox state must be fully reset between tests. Run this
before each new section:

```bash
podman exec openshell-e2e bash -c '
  pids=$(ss -tlnp | grep 17670 | grep -oP "pid=\K[0-9]+" 2>/dev/null)
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null
  pkill -9 -f "podman system" 2>/dev/null
  sleep 1
  podman rm -af 2>/dev/null
  podman network rm openshell 2>/dev/null
  rm -rf ~/.config/openshell ~/.local/state/openshell
'
```

## Models

Use haiku to keep cost down. The model name format varies by harness:

| Harness     | Vertex AI model                             | API key model                          |
|-------------|---------------------------------------------|----------------------------------------|
| Claude Code | `claude-haiku-4-5`                          | `claude-haiku-4-5`                     |
| OpenCode    | `google-vertex/claude-haiku-4-5@20251001`   | `anthropic/claude-haiku-4-5-20251001`  |
| Cursor      | (not supported via Vertex)                  | `claude-4.6-sonnet-medium-thinking`    |

## Known issue: Claude Code + Vertex AI

Claude Code sends a `context_management` field that Vertex AI's rawPredict
rejects with HTTP 400. This is an upstream OpenShell bug
([NVIDIA/OpenShell#1752](https://github.com/NVIDIA/OpenShell/pull/1752)).
The workaround is `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`, which
agentic-ci now sets automatically in the sandbox env script.

See `docs/openshell-vertex-streaming-bug.md` for details.

---

## Section A: Claude Code + Vertex AI

Requires GCP ADC credentials and `ANTHROPIC_VERTEX_PROJECT_ID`.

### A1. Run

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" \
  -e SANDBOX_IMAGE="$CLAUDE_SANDBOX_IMAGE" \
  openshell-e2e bash -c '
    agentic-ci run \
      --backend openshell \
      --harness claude-code \
      --image "$SANDBOX_IMAGE" \
      --model claude-haiku-4-5 \
      --no-otel \
      "Respond with exactly: A1_OK"
  '
```

Verify:
- Output shows `Auth: Vertex AI`
- Output shows `Starting OpenShell gateway` (or `already running`)
- Output shows `Creating Vertex AI provider` (or `already exists`)
- Output shows `Created sandbox: ci`
- Output shows `Running Claude Code (claude-haiku-4-5) via openshell backend`
- Claude's response contains `A1_OK`
- Token metrics show non-zero counts and cost is non-zero (e.g. `$0.04`)
- `Agent exit code: 0`
- `Sandbox deleted` and `Gateway stopped` at the end

---

## Section B: Claude Code + API Key

Run cleanup first.

### B1. Run

```bash
podman exec \
  -e "ANTHROPIC_API_KEY=$(cat "$API_KEY_FILE")" \
  -e OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" \
  -e SANDBOX_IMAGE="$CLAUDE_SANDBOX_IMAGE" \
  openshell-e2e bash -c '
    agentic-ci run \
      --backend openshell \
      --harness claude-code \
      --image "$SANDBOX_IMAGE" \
      --model claude-haiku-4-5 \
      --no-otel \
      "Respond with exactly: B1_OK"
  '
```

Verify:
- Output shows `Auth: API key`
- Output shows `Creating Anthropic API key provider`
- Claude's response contains `B1_OK`
- Token metrics show non-zero counts and cost is non-zero
- `Agent exit code: 0`

---

## Section C: OpenCode + Vertex AI

Requires GCP ADC credentials and `ANTHROPIC_VERTEX_PROJECT_ID`.

Run cleanup first.

### C1. Run

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" \
  -e SANDBOX_IMAGE="$OPENCODE_SANDBOX_IMAGE" \
  openshell-e2e bash -c '
    agentic-ci run \
      --backend openshell \
      --harness opencode \
      --image "$SANDBOX_IMAGE" \
      --model "google-vertex/claude-haiku-4-5@20251001" \
      --no-otel \
      "Respond with exactly: C1_OK"
  '
```

Verify:
- Output shows `Harness: OpenCode` and `Auth: Vertex AI`
- Inference configured with `Model: claude-haiku-4-5` (prefix stripped)
- `Agent exit code: 0`

---

## Section D: OpenCode + API Key

Run cleanup first.

### D1. Run

```bash
podman exec \
  -e "ANTHROPIC_API_KEY=$(cat "$API_KEY_FILE")" \
  -e OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" \
  -e SANDBOX_IMAGE="$OPENCODE_SANDBOX_IMAGE" \
  openshell-e2e bash -c '
    agentic-ci run \
      --backend openshell \
      --harness opencode \
      --image "$SANDBOX_IMAGE" \
      --model "anthropic/claude-haiku-4-5-20251001" \
      --no-otel \
      "Respond with exactly: D1_OK"
  '
```

Verify:
- Output shows `Harness: OpenCode` and `Auth: API key`
- Agent response contains `D1_OK`
- `Agent exit code: 0`

---

## Section E: Workdir round-trip

Verifies that the OpenShell backend uploads the workdir into the sandbox,
the agent can modify files inside it, and changes are downloaded back to
the host after the run completes. Uses Vertex AI auth and Claude Code.

Run cleanup first.

### E1. Prepare workdir

Create a temporary directory with a seed file:

```bash
podman exec openshell-e2e bash -c '
  mkdir -p /tmp/workdir-test
  echo red > /tmp/workdir-test/color.txt
'
```

Verify:

```bash
podman exec openshell-e2e cat /tmp/workdir-test/color.txt
```

Should print `red`.

### E2. Run agent to modify the file

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" \
  -e SANDBOX_IMAGE="$CLAUDE_SANDBOX_IMAGE" \
  openshell-e2e bash -c '
    agentic-ci run \
      --backend openshell \
      --harness claude-code \
      --image "$SANDBOX_IMAGE" \
      --model claude-haiku-4-5 \
      --workdir /tmp/workdir-test \
      --no-otel \
      "Overwrite the file color.txt with exactly the word blue (no newline, no quotes). Do not create any other files."
  '
```

Verify:
- Output shows `Uploading workdir`
- Output shows `Downloading workdir`
- `Agent exit code: 0`

### E3. Verify workdir was updated on the host

```bash
podman exec openshell-e2e cat /tmp/workdir-test/color.txt
```

The file should now contain `blue`, not `red`. This confirms the full
round-trip: the workdir was uploaded into the sandbox, the agent modified
it, and the changes were downloaded back to the host.

### E4. Clean up workdir

```bash
podman exec openshell-e2e rm -rf /tmp/workdir-test
```

---

## Section F: OTEL telemetry collection

Verifies that the sandbox-local OTEL collector receives metrics from the
agent and prints a token/cost summary. Uses Vertex AI auth and Claude Code
(the only harness that supports OTEL).

The OpenShell sandbox network isolation prevents reaching an external OTEL
collector, so agentic-ci embeds a lightweight OTLP receiver inside the
sandbox on localhost. After the run, the OTEL log is downloaded from the
sandbox and the summary is printed on the host.

Run cleanup first.

### F1. Run with OTEL enabled

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" \
  -e SANDBOX_IMAGE="$CLAUDE_SANDBOX_IMAGE" \
  openshell-e2e bash -c '
    cd /tmp/e2e-workdir && \
    agentic-ci run \
      --backend openshell \
      --harness claude-code \
      --image "$SANDBOX_IMAGE" \
      --model claude-haiku-4-5 \
      "Respond with exactly: F1_OK"
  '
```

Note: no `--no-otel` flag.

Verify:
- Output shows `Running Claude Code (claude-haiku-4-5) via openshell backend`
- Agent runs and completes with `F1_OK` in the response
- Output shows `Token/Cost Summary (OpenTelemetry)` section
- Token counts are non-zero (input tokens, output tokens, cache)
- Cost is non-zero (e.g. `$0.04`)
- `Agent exit code: 0`
- `Sandbox deleted` and `Gateway stopped` at the end

### F2. Verify trace records for mlflow-push

Check that the OTEL JSONL log contains `/v1/traces` records — these are
what `agentic-ci mlflow-push` needs. The JSONL file is copied to the
CI artifact directory or stays in the run directory:

```bash
podman exec openshell-e2e bash -c '
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
  podman exec openshell-e2e bash -c '
    JSONL=$(ls -t /tmp/agentic-ci-run.*/claude-otel.jsonl 2>/dev/null | head -1)
    grep "/v1/traces" "$JSONL" | head -1 | python3 -m json.tool
  '
  ```
  - Record has `"path"` containing `/v1/traces`
  - Record has `"payload"` with `"resourceSpans"` array

---

## Section G: Skill execution — Vertex AI

Verifies that a plugin skill loads and executes correctly inside the
sandbox. Uses the `git-shallow-clone` skill from the `odh-ai-helpers`
plugin to clone a repo into `/tmp` and confirm the result.

Requires GCP ADC credentials and `ANTHROPIC_VERTEX_PROJECT_ID`.

Run cleanup first.

### G1. Run

```bash
podman exec \
  -e ANTHROPIC_VERTEX_PROJECT_ID=<your-project-id> \
  -e CLOUD_ML_REGION=global \
  -e OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" \
  -e SANDBOX_IMAGE="$CLAUDE_SANDBOX_IMAGE" \
  openshell-e2e bash -c '
    mkdir -p /tmp/e2e-workdir && cd /tmp/e2e-workdir && \
    agentic-ci run \
      --backend openshell \
      --harness claude-code \
      --image "$SANDBOX_IMAGE" \
      --model claude-haiku-4-5 \
      --no-otel \
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

---

## Section H: Skill execution — API Key

Same test as Section G but with API key auth.

Run cleanup first.

### H1. Run

```bash
podman exec \
  -e "ANTHROPIC_API_KEY=$(cat "$API_KEY_FILE")" \
  -e OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE" \
  -e SANDBOX_IMAGE="$CLAUDE_SANDBOX_IMAGE" \
  openshell-e2e bash -c '
    mkdir -p /tmp/e2e-workdir && cd /tmp/e2e-workdir && \
    agentic-ci run \
      --backend openshell \
      --harness claude-code \
      --image "$SANDBOX_IMAGE" \
      --model claude-haiku-4-5 \
      --no-otel \
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

---

## Final cleanup

```bash
podman rm -f openshell-e2e
```

## Running the full suite

Execute sections in order (A through H), running the cleanup step before each
section. Skip sections whose prerequisites are not met. If any step fails,
check the gateway log inside the container:

```bash
podman exec openshell-e2e bash -c 'cat ~/.local/state/openshell/gateway-*.log'
```
