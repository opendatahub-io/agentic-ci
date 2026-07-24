# agentic-ci

[![PyPI version](https://img.shields.io/pypi/v/agentic-ci)](https://pypi.org/project/agentic-ci/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/agentic-ci)](https://pypi.org/project/agentic-ci/)
[![CI](https://github.com/opendatahub-io/agentic-ci/actions/workflows/ci.yml/badge.svg)](https://github.com/opendatahub-io/agentic-ci/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/opendatahub-io/agentic-ci)](https://github.com/opendatahub-io/agentic-ci/blob/main/LICENSE)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/agentic-ci)](https://pypi.org/project/agentic-ci/)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://opendatahub-io.github.io/agentic-ci/)

Run AI coding agents in sandboxed CI environments with streaming output
and telemetry. Supports multiple agent harnesses (Claude Code, OpenCode, Cursor)
and isolation backends so you can choose the right tradeoff between
simplicity and security.

## Backends

### Local

Runs the agent directly in the current environment with no container
or sandbox layer. The agent binary must already be installed and on
PATH. Environment variables (auth, OTEL, model) are set automatically
by the harness.

Good for: running inside an existing CI container (e.g. a Prow step
image) where the agent CLI is pre-installed and an extra isolation
layer is unnecessary.

Requires: the agent CLI on PATH (e.g. `claude`).

### Podman (default)

Runs the agent inside a Podman container. Each `run` creates a fresh
container that auto-deletes on exit. The work directory is mounted
into the container and gcloud credentials are mounted read-only.

**Important:** The Podman backend provides only basic container-level
isolation. It uses `--network host`, so the agent has unrestricted
network access. There is no filesystem sandboxing beyond the container
boundary itself and no network policy enforcement. Use the OpenShell
backend if you need stronger security controls.

Good for: local development, CI runners that already have Podman,
quick one-off runs in trusted environments.

Requires: `podman`, a container image with the agent CLI installed
(e.g. `ghcr.io/opendatahub-io/ai-helpers:latest`).

### OpenShell

Runs the agent inside an [OpenShell](https://github.com/NVIDIA/OpenShell)
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
uv tool install agentic-ci
# OR
pip install agentic-ci
```

## Usage

### Run a prompt

```bash
# Local (direct execution, no container)
agentic-ci run --backend local "Fix the flaky test in test_auth.py"

# Podman (default backend)
agentic-ci run "Fix the flaky test in test_auth.py" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest

# OpenShell
agentic-ci run --backend openshell "Fix the flaky test in test_auth.py"
```

### Setup and stop

`setup` creates and starts the sandbox environment. `stop` tears it
down. `run` auto-calls `setup` if the sandbox isn't already running.

```bash
# Start the sandbox
agentic-ci setup --image ghcr.io/opendatahub-io/ai-helpers:latest

# Run multiple prompts in the same sandbox (use --keep to prevent auto-teardown)
agentic-ci run "Fix the flaky test" --keep \
    --image ghcr.io/opendatahub-io/ai-helpers:latest
agentic-ci run "Update the changelog" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest

# Tear down the sandbox
agentic-ci stop
```

### Options

```
agentic-ci {setup,run,stop} [options]
```

| Flag | Default | Description |
|---|---|---|
| `--backend` | `podman` | Sandbox backend to use |
| `--harness` | `claude-code` | Agent harness (`claude-code`, `opencode`, or `cursor`) |
| `--workdir PATH` | `.` | Working directory to mount |
| `--image IMAGE` | — | Container or sandbox base image |
| `--model MODEL` | harness-dependent | Agent model (`run` only). Defaults to `claude-opus-4-6` for Claude Code, `google-vertex/claude-opus-4-6@default` for OpenCode, `claude-4.6-sonnet-medium-thinking` for Cursor |
| `--keep` | off | Keep the sandbox running after the run completes (`run` only) |
| `--no-streaming` | off | Disable parsed stream output; agent output is printed raw (`run` only) |
| `--no-otel` | off | Disable OTEL telemetry collection (`run` only) |
| `--pre-gates GATES` | — | Comma-separated pre-agent gates (`run` only) |
| `--post-gates GATES` | — | Comma-separated post-agent gates (`run` only) |
| `--policy PATH` | — | OpenShell policy file override (`openshell` backend only) |
| `--timeout SECS` | `1200` | Container timeout (`podman` backend only) |

Extra arguments after the prompt are passed through to the Claude CLI.

### Examples

```bash
# Local backend with extra Claude args (everything after -- is passed through)
# Note: build_args() sets --permission-mode bypassPermissions by default;
# pass --permission-mode default to restrict tools via --allowedTools
agentic-ci run --backend local \
    "Fix the flaky test" \
    -- --permission-mode default --allowedTools "Bash Read Edit" --max-turns 10 --verbose

# Local backend with --continue for multi-stage flows
agentic-ci run --backend local "Summarize your findings" \
    -- --continue --max-turns 5

# Use a specific model
agentic-ci run "Update the changelog" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest \
    --model claude-sonnet-4-6

# Disable parsed stream output (prints raw agent output)
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
agentic-ci run --backend openshell "Deploy staging" \
    --policy custom-policy.yml

# OpenShell with repo-level policy (auto-discovered from
# .agentic-ci/openshell-policy.yml in the workdir)
agentic-ci run --backend openshell "Add input validation"
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

Pre-agent gates are supported via `--pre-gates` with custom
implementations (e.g. filtering by comment domain or author).

All required environment variables are validated before any gate runs.
If any are missing, the CLI exits immediately with a clear error listing
every missing variable and which gate needs it.

## Credentials

Authentication is auto-detected from the environment and logged at
startup. Claude Code and OpenCode support Anthropic API key and Vertex
AI modes; Cursor uses its own API key.

### Anthropic API key (direct)

Set `ANTHROPIC_API_KEY` in the environment. No gcloud credentials
are needed; the key is passed directly to the agent inside the
container or sandbox. Vertex-specific env vars and credential mounts
are skipped.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
agentic-ci run "Fix the bug" --image ghcr.io/opendatahub-io/ai-helpers:latest
```

### Cursor API key

Set `CURSOR_API_KEY` in the environment. This is the only authentication
mode supported by the Cursor harness (Vertex AI / GCP auth is not available
for Cursor).

```bash
export CURSOR_API_KEY=crsr_...
agentic-ci run "Fix the bug" --harness cursor
```

### Vertex AI (default)

When `ANTHROPIC_API_KEY` is not set, both backends use Vertex AI for
Claude API access via gcloud Application Default Credentials.

The **podman** backend checks credentials in this order:

1. `GCLOUD_CREDENTIALS` env var (raw JSON or base64-encoded)
2. `GCP_SERVICE_ACCOUNT_KEY` env var (file path, raw JSON, or base64-encoded)
3. `~/.config/gcloud/application_default_credentials.json`
4. Path in `GOOGLE_APPLICATION_CREDENTIALS` env var

The **openshell** backend uploads the local ADC file
(`~/.config/gcloud/application_default_credentials.json` or
`GOOGLE_APPLICATION_CREDENTIALS`) into the sandbox.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | -- | Anthropic API key. When set, uses direct API auth instead of Vertex AI |
| `CLAUDE_MODEL` | `claude-opus-4-6` | Default model for Claude Code harness (overridden by `--model`) |
| `CLAUDE_CONTAINER_IMAGE` | — | Default container image for Claude Code harness |
| `OPENCODE_MODEL` | `google-vertex/claude-opus-4-6@default` | Default model for OpenCode harness (overridden by `--model`) |
| `OPENCODE_CONTAINER_IMAGE` | — | Default container image for OpenCode harness |
| `CURSOR_MODEL` | `claude-4.6-sonnet-medium-thinking` | Default model for Cursor harness (overridden by `--model`) |
| `CURSOR_CONTAINER_IMAGE` | — | Default container image for Cursor harness |
| `CURSOR_API_KEY` | — | Cursor API key for Cursor harness authentication |
| `ANTHROPIC_VERTEX_PROJECT_ID` | — | Vertex AI project ID |
| `GCP_PROJECT_ID` | — | Fallback for `ANTHROPIC_VERTEX_PROJECT_ID` |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project ID (OpenCode uses this before falling back to `ANTHROPIC_VERTEX_PROJECT_ID`) |
| `CLOUD_ML_REGION` | `global` | Vertex AI region |
| `VERTEX_LOCATION` | — | Vertex AI region (OpenCode uses this before falling back to `CLOUD_ML_REGION`) |
| `GCLOUD_CREDENTIALS` | — | Raw JSON or base64 gcloud credentials |
| `GCP_SERVICE_ACCOUNT_KEY` | — | Service account key: file path, raw JSON, or base64-encoded JSON |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to ADC credentials file |
| `OPENSHELL_SUPERVISOR_IMAGE` | `openshell/supervisor:dev` | OpenShell supervisor image (`openshell` backend only) |

## Streaming Output

By default, agent output is parsed into human-readable CI logs with:

- Colored ANSI output (thinking in red, tool calls in gray)
- Tool call summaries (bash commands, file paths, agent dispatches)
- Token count display with throughput rate
- OTEL token/cost summary at completion

Disable with `--no-streaming` to skip the parsed output and print raw
agent output, or `--no-otel` to skip the token/cost summary.

## Python API

```python
from agentic_ci.backends import create_backend
from agentic_ci.harness import create_harness

harness = create_harness("claude-code")

# Podman backend
backend = create_backend(
    "podman", harness=harness, workdir="/path/to/repo", image="my-image:latest"
)
backend.setup()
rc = backend.run(prompt="Fix the bug", model="claude-sonnet-4-6")
backend.stop()

# Local backend (no container)
backend = create_backend("local", harness=harness, workdir="/path/to/repo")
backend.setup()
rc = backend.run(prompt="Fix the bug", model="claude-sonnet-4-6", extra_args=["--max-turns", "10"])
backend.stop()
```

## Additional Modules

The package includes several library modules used by downstream
pipelines:

- **`agentic_ci.jira`** — Jira REST API client with `acli` delegation,
  ADF (Atlassian Document Format) conversion, and rate limiting.
- **`agentic_ci.git`** — Git operations (clone, branch, push, diff,
  commit info extraction) with security hardening.
- **`agentic_ci.pipeline`** — GitLab child pipeline YAML generation
  with hash-based slot distribution.
- **`agentic_ci.verdict`** — Structured verdict JSON schema validation.

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
    verdict_loader=lambda work_dir: json.loads((work_dir / "verdict.json").read_text()),
    label_applier=lambda ticket_key, verdict, **kw: print(f"[{ticket_key}] verdict: {verdict}"),
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

### Extra Skills (Extension Hooks)

`extra_skills` lets you configure additional skills that the agent should
run at specific hook points during the pipeline (e.g., run a preflight
review after implementing a fix).

```python
config = SkillConfig(
    skill_name="autofix-resolve",
    extra_skills=[
        {"name": "preflight", "args": "--local --fix", "hooks": ["post_implement"]},
        {"name": "lint-check"},
    ],
    context_dir=".autofix-context",  # where config.json is written (default: ".context")
)
```

Each entry is an object with `name` (required), `args` (optional), and `hooks` (optional).

When `extra_skills` is non-empty, `run_skill()` writes
`{context_dir}/config.json` before launching the container:

```json
{
  "extra_skills": [
    {"name": "preflight", "args": "--local --fix", "hooks": ["post_implement"]},
    {"name": "lint-check"}
  ]
}
```

**Important:** `run_skill()` only writes the config file — it does not
execute the extra skills directly. The orchestrator skill (the one
launched by `run_skill()`) must include instructions in its SKILL.md
to read `{context_dir}/config.json` and invoke each extension at the
appropriate hook point. The extra skills themselves don't need any
awareness of this file. `context_dir` is validated to stay within
`work_dir` (rejects path traversal and symlinks).

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
    prompt_builder=_build_prompt,  # Jira-specific prompt
    context_writer=_write_context,  # Writes ticket.json to .autofix-context/
    verdict_loader=_load_verdict,  # Reads .autofix-verdict.json
    label_applier=_apply_labels,  # Manages jira-autofix-* labels
    cost_formatter=_format_otel_cost,  # Formats cost for Jira comments
    post_gates=[_autofix_post_gate],  # Commit author check, sensitive files, gitleaks
)
```

## Container Images

Pre-built container images for running AI coding agents in CI are
published to `quay.io/aipcc/agentic-ci/`:

| Image | Description |
|-------|-------------|
| `claude-runner` | Claude Code CLI with pre-installed skills |
| `opencode-runner` | OpenCode CLI with pre-installed skills |
| `claude-sandbox` | Claude Code sandbox for OpenShell |
| `opencode-sandbox` | OpenCode sandbox for OpenShell |
| `cursor-runner` | Cursor Agent CLI runner |
| `cursor-sandbox` | Cursor sandbox for OpenShell |
| `podman` | CI environment with podman, gh, glab, gitleaks, acli |
| `openshell` | CI environment with OpenShell gateway + podman |

Images are rebuilt daily via GitHub Actions and version-managed by
Renovate. See [Container Image docs](https://opendatahub-io.github.io/agentic-ci/image/)
for usage details.

```bash
make claude-build              # build Claude Code runner image locally
make opencode-build            # build OpenCode runner image locally
make cursor-build              # build Cursor runner image locally
make ci-build                  # build CI podman image locally
make openshell-claude-build    # build Claude sandbox locally
make openshell-opencode-build  # build OpenCode sandbox locally
make openshell-cursor-build    # build Cursor sandbox locally
make openshell-ci-build        # build OpenShell CI image locally
```

## Documentation

API reference documentation is auto-generated from docstrings and
published to [GitHub Pages](https://opendatahub-io.github.io/agentic-ci/).

To build the docs locally:

```bash
tox -e docs
```

Or to preview with live reload:

```bash
uv run --with '.[docs]' mkdocs serve
```

