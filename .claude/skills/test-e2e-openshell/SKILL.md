---
name: test-e2e-openshell
description: Run end-to-end tests for the openshell backend using real container and API calls
---

# End-to-End OpenShell Backend Test

Run full lifecycle tests of the OpenShell backend across harnesses and auth modes.
All tests run inside a privileged container.

Each section below is independent. Run whichever sections match your environment and skip the rest.

## Before you start

Ask the user for three things:

1. **CI image** — a local container image with openshell, openshell-gateway, podman, and uv installed. This is the outer container that runs the gateway and manages sandboxes. Store as `$CI_IMAGE`.

2. **Sandbox images** — container images for the agent sandbox (one for Claude Code, one for OpenCode). These must have a `sandbox` user and `iproute` installed per OpenShell conventions. Can be registry references (e.g. `quay.io/...`) or locally-built images. Store as `$CLAUDE_SANDBOX_IMAGE` and `$OPENCODE_SANDBOX_IMAGE`.

3. **API key file** — path to a local file containing an Anthropic API key (one line, no whitespace). Needed for Sections B and D. Store as `$API_KEY_FILE`.

Per-section requirements:
- **Vertex AI auth** (Sections A, C): GCP ADC credentials at `~/.config/gcloud/application_default_credentials.json` and `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION` set in the environment.
- **API key auth** (Sections B, D): The API key file above.

## Container setup

OpenShell requires `--privileged` for nested podman (user namespace mapping,
overlay mounts, network namespace creation). All agentic-ci commands run
inside this outer container.

### Start the test container

```bash
podman rm -f openshell-e2e 2>/dev/null || true
podman run -d --name openshell-e2e \
  --privileged \
  -v "$(pwd):/workspace:z" \
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
  cd /workspace && uv pip install --system --no-cache -e .
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
- Token metrics show non-zero counts, cost around `$0.04`
- `Agent exit code: 0`
- `Sandbox deleted` and `Gateway stopped` at the end

---

## Section B: Claude Code + API Key

Run cleanup first.

### B1. Run

```bash
podman exec \
  -e "ANTHROPIC_API_KEY=$(cat "$API_KEY_FILE")" \
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
- Token metrics show non-zero counts and cost
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

## Final cleanup

```bash
podman rm -f openshell-e2e
```

## Running the full suite

Execute sections in order (A through D), running the cleanup step before each
section. Skip sections whose prerequisites are not met. If any step fails,
check the gateway log inside the container:

```bash
podman exec openshell-e2e bash -c 'cat ~/.local/state/openshell/gateway-*.log'
```
