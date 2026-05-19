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
| `--pre-gates GATES` | — | Comma-separated pre-agent gates (`run` only) |
| `--post-gates GATES` | — | Comma-separated post-agent gates (`run` only) |
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

# Run with post-agent gates
export TICKET_KEY=AIPCC-123
export BOT_EMAIL=bot@ci.com
agentic-ci run "Fix the bug" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest \
    --post-gates sensitive-files,commit-author,commit-message-key,gitleaks

# OpenShell with custom policy
agentic-ci --backend openshell run "Deploy staging" \
    --policy custom-policy.yml

# OpenShell with repo-level policy (auto-discovered from
# .agentic-ci/openshell-policy.yml in the workdir)
agentic-ci --backend openshell run "Add input validation"
```

### Gates

Gates validate data before and after an AI agent runs. Pre-gates can
block execution early; post-gates validate output to catch dangerous
changes. Gates read their configuration from environment variables.

**Built-in post-agent gates:**

| Name | Required Env Vars | Description |
|---|---|---|
| `sensitive-files` | — | Block commits touching `.env`, `*.pem`, `*.key`, etc. |
| `commit-author` | `BOT_EMAIL` | Verify commit author matches expected bot email |
| `commit-message-key` | `TICKET_KEY` | Verify ticket key appears in commit message |
| `gitleaks` | — | Scan new commits for secrets using gitleaks |

All required environment variables are validated before any gate runs.
If any are missing, the CLI exits immediately with a clear error listing
every missing variable and which gate needs it.

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

## Building a Pipeline with the Generic Skill Runner

`agentic-ci` provides a generic skill runner framework that any project can
use to build its own AI-powered CI pipeline. You define what happens at each
stage via callable hooks; the framework handles container execution, retries,
OTEL cost tracking, and gate orchestration.

### Quick Start

```python
import json
from pathlib import Path
from agentic_ci.skill import SkillConfig, run_skill

config = SkillConfig(
    skill_name="my-review",
    prompt_builder=lambda ticket_key, mode, skill_name, **kw: (
        f"Use the /{skill_name} skill to review ticket {ticket_key}."
    ),
    verdict_loader=lambda work_dir: json.loads(
        (work_dir / "verdict.json").read_text()
    ),
    label_applier=lambda ticket_key, verdict, **kw: (
        print(f"[{ticket_key}] verdict: {verdict}")
    ),
)

rc = run_skill(
    config,
    ticket_key="PROJ-123",
    work_dir=Path("/tmp/work"),
    config_dir=Path("/tmp/config"),
)
```

### SkillConfig Hooks

All domain-specific behavior is injected via hooks on `SkillConfig`:

| Hook | Signature | Purpose |
|------|-----------|---------|
| `prompt_builder` | `(ticket_key, mode, skill_name, **kw) -> str` | Build the prompt sent to Claude |
| `context_writer` | `(ticket_key, ticket, mode, work_dir, **kw) -> None` | Write context files before the run |
| `verdict_loader` | `(work_dir) -> dict` | Load the agent's verdict after the run |
| `verdict_path_fn` | `(work_dir) -> Path` | Where to find the verdict file |
| `label_applier` | `(ticket_key, verdict, mode, work_dir, **kw) -> None` | Apply labels/transitions after the run |
| `cost_formatter` | `(cost_data) -> str \| None` | Format OTEL cost data for display |
| `extension_config_writer` | `(ticket_key, ticket, config, work_dir, **kw) -> None` | Write extra config (e.g. Claude extensions) |

### Pipeline Flow

`run_skill()` executes this sequence:

1. **Pre-gates** -- each `pre_gates` callable can block the run early (returns a message to skip, `None` to continue)
2. **Context** -- `context_writer` writes ticket data and supporting files
3. **Extension config** -- `extension_config_writer` sets up Claude plugins/skills
4. **Prompt** -- `prompt_builder` produces the prompt string
5. **Container** -- launches Claude via `PodmanBackend` (or a custom `container_runner`)
6. **Retry** -- transient failures (exit 124/137/143) retry once if `mode` is in `retryable_modes`
7. **Cost** -- parses OTEL metrics from the run directory
8. **Post-gates** -- each `post_gates` callable validates the output (e.g. sensitive file check, gitleaks)
9. **Verdict** -- `verdict_loader` reads the agent's structured output
10. **Report** -- `label_applier` applies labels, posts comments, transitions tickets

### Example: jira-autofix

The [jira-autofix](https://gitlab.com/redhat/rhel-ai/agentic-ci/jira-autofix)
project uses this framework to build an automated Jira bug-fix pipeline:

```python
config = SkillConfig(
    skill_name="autofix-resolve",
    prompt_builder=_build_prompt,         # Jira-specific prompt
    context_writer=_write_context,        # Writes ticket.json to .autofix-context/
    verdict_loader=_load_verdict,          # Reads .autofix-verdict.json
    label_applier=_apply_labels,          # Manages jira-autofix-* labels
    cost_formatter=_format_otel_cost,     # Formats cost for Jira comments
    post_gates=[_autofix_post_gate],      # Commit author check, sensitive files, gitleaks
)
```

