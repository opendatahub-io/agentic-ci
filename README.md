# agentic-ci

Run Claude Code in sandboxed CI environments with streaming output and
telemetry. Supports multiple isolation backends so you can choose the
right tradeoff between simplicity and security.

## Backends

### Podman (default)

Runs Claude inside a Podman container. Each `run` creates a fresh
container that auto-deletes on exit. The work directory is mounted
into the container and gcloud credentials are mounted read-only.

**Important:** The Podman backend provides only basic container-level
isolation. It uses `--network host`, so the agent has unrestricted
network access. There is no filesystem sandboxing beyond the container
boundary itself and no network policy enforcement. Use the OpenShell
backend if you need stronger security controls.

Good for: local development, CI runners that already have Podman,
quick one-off runs in trusted environments.

Requires: `podman`, a container image with Claude Code installed
(e.g. `ghcr.io/opendatahub-io/ai-helpers:latest`).

### OpenShell

Runs Claude inside an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox with network policy enforcement, Landlock-based filesystem
access control, and fine-grained endpoint restrictions. Network
policies limit which hosts the agent can reach (e.g. only Vertex AI,
GitHub, PyPI) and filesystem policies restrict which paths are
writable. An embedded gateway starts per CI job — no external
infrastructure required.

Good for: production CI where you need to control what the agent can
access on the network and filesystem.

Requires: `openshell` and `openshell-gateway` installed on the host.

## Install

```bash
pip install ./agentic-ci/
```

## Usage

### Run a prompt

```bash
# Podman (default backend)
agentic-ci run "Fix the flaky test in test_auth.py" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest

# OpenShell
agentic-ci --backend openshell run "Fix the flaky test in test_auth.py"
```

### Setup and stop

`setup` creates and starts the sandbox environment. `stop` tears it
down. `run` auto-calls `setup` if the sandbox isn't already running.

```bash
# Start the sandbox
agentic-ci setup --image ghcr.io/opendatahub-io/ai-helpers:latest

# Run multiple prompts in the same sandbox
agentic-ci run "Fix the flaky test" --image ghcr.io/opendatahub-io/ai-helpers:latest
agentic-ci run "Update the changelog" --image ghcr.io/opendatahub-io/ai-helpers:latest

# Tear down the sandbox
agentic-ci stop --image ghcr.io/opendatahub-io/ai-helpers:latest
```

### Options

```
agentic-ci [--backend {podman,openshell}] {setup,run,stop} [options]
```

| Flag | Default | Description |
|---|---|---|
| `--backend` | `podman` | Sandbox backend to use |
| `--workdir PATH` | `.` | Working directory to mount |
| `--image IMAGE` | — | Container or sandbox base image |
| `--model MODEL` | `claude-opus-4-6` | Claude model (`run` only) |
| `--no-streaming` | off | Disable pretty-printed stream output (`run` only) |
| `--no-otel` | off | Disable OTEL telemetry collection (`run` only) |
| `--policy PATH` | — | OpenShell policy file override (`openshell` backend only) |
| `--timeout SECS` | `1200` | Container timeout (`podman` backend only) |

Extra arguments after the prompt are passed through to the Claude CLI.

### Examples

```bash
# Use a specific model
agentic-ci run "Update the changelog" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest \
    --model claude-sonnet-4-6

# Disable streaming for raw output
agentic-ci run "Run the test suite" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest \
    --no-streaming

# Disable telemetry
agentic-ci run "Fix lint errors" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest \
    --no-otel

# OpenShell with custom policy
agentic-ci --backend openshell run "Deploy staging" \
    --policy custom-policy.yml

# OpenShell with repo-level policy (auto-discovered from
# .agentic-ci/openshell-policy.yml in the workdir)
agentic-ci --backend openshell run "Add input validation"
```

## Credentials

Both backends use Vertex AI for Claude API access via gcloud
Application Default Credentials.

The **podman** backend checks credentials in this order:

1. `GCLOUD_CREDENTIALS` env var (raw JSON or base64-encoded)
2. `GCP_SERVICE_ACCOUNT_KEY` env var (base64-encoded)
3. `~/.config/gcloud/application_default_credentials.json`
4. Path in `GOOGLE_APPLICATION_CREDENTIALS` env var

The **openshell** backend uploads the local ADC file
(`~/.config/gcloud/application_default_credentials.json` or
`GOOGLE_APPLICATION_CREDENTIALS`) into the sandbox.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_MODEL` | `claude-opus-4-6` | Default model (overridden by `--model`) |
| `CLAUDE_CONTAINER_IMAGE` | — | Default container image for podman backend |
| `ANTHROPIC_VERTEX_PROJECT_ID` | — | Vertex AI project ID |
| `GCP_PROJECT_ID` | — | Fallback for `ANTHROPIC_VERTEX_PROJECT_ID` |
| `CLOUD_ML_REGION` | `global` | Vertex AI region |
| `GCLOUD_CREDENTIALS` | — | Raw JSON or base64 gcloud credentials |
| `GCP_SERVICE_ACCOUNT_KEY` | — | Base64-encoded service account key |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to ADC credentials file |
| `OPENSHELL_SUPERVISOR_IMAGE` | `openshell/supervisor:dev` | OpenShell supervisor image (openshell backend only) |

## Streaming Output

By default, Claude's stream-json output is parsed into human-readable
CI logs with:

- Colored ANSI output (thinking in red, tool calls in gray)
- Tool call summaries (bash commands, file paths, agent dispatches)
- Token count display with throughput rate
- OTEL token/cost summary at completion

Disable with `--no-streaming` for raw output or `--no-otel` to skip
the summary.

## Python API

```python
from agentic_ci.backends import create_backend

backend = create_backend("podman", workdir="/path/to/repo", image="my-image:latest")
backend.setup()
rc = backend.run(prompt="Fix the bug", model="claude-sonnet-4-6")
backend.stop()
```
