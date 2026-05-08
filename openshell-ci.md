# agentic-ci: OpenShell CI Wrapper Design

## Overview

Refactor agentic-ci from a standalone Claude Code CI runner into a thin orchestration layer over OpenShell. agentic-ci abstracts OpenShell's gateway, sandbox, and policy internals so that users of a purpose-built CI base image can run a single command to execute Claude Code in a sandboxed environment with streaming output and OTEL telemetry.

The base image ships with `openshell`, `agentic-ci`, and CI tools (`gh`, `glab`, `acli`, etc.) pre-installed. Users only interact with `agentic-ci`.

## Constraints

- **Claude Code only** for now. Other agents (OpenCode, Codex, Copilot) can be added later since OpenShell supports them.
- **Vertex AI** for Claude API access. No provider setup needed.
- **Zero Python dependencies** — stdlib only, shell out to `openshell` CLI.
- **Embedded gateway** — started locally per CI job, no external gateway infrastructure required.

## CLI Interface

### `agentic-ci setup`

Prepares the environment: starts the OpenShell gateway and creates a sandbox.

```
agentic-ci setup [--policy PATH] [--workdir PATH]
```

- Starts an embedded OpenShell gateway via `openshell gateway start`.
- Resolves policy (see Policy Resolution below).
- Creates a sandbox with Claude Code as the agent and applies the resolved policy.
- Idempotent — checks `openshell status` first; skips if gateway/sandbox are already running.

### `agentic-ci run`

Executes a prompt inside the sandbox with streaming output and OTEL telemetry.

```
agentic-ci run [--no-streaming] [--no-otel] [--model MODEL]
              [--policy PATH] [--workdir PATH]
              PROMPT [-- EXTRA_CLAUDE_ARGS...]
```

- Checks `openshell status` — if not set up, runs setup logic first (passing through `--policy` and `--workdir`).
- Starts the OTEL collector on a dynamic port (unless `--no-otel`).
- Builds the Claude CLI invocation with OTEL environment variables.
- Executes Claude inside the sandbox via `openshell sandbox exec`.
- Pipes stdout through StreamProcessor for real-time pretty-printing (unless `--no-streaming`).
- Prints token/cost summary on completion.
- Copies artifacts to CI workspace if `GITHUB_WORKSPACE` or `CI_PROJECT_DIR` is set.

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--policy PATH` | (none) | Explicit policy file override |
| `--workdir PATH` | `.` | Working directory (for policy discovery and execution) |
| `--no-streaming` | streaming on | Disable pretty-printed stream output |
| `--no-otel` | OTEL on | Disable OTEL telemetry collection |
| `--model MODEL` | `$CLAUDE_MODEL` or `claude-opus-4-6` | Claude model to use |

Extra arguments after `--` are passed through to the Claude CLI.

## Policy Resolution

Policy is resolved in priority order:

1. `--policy PATH` flag (explicit override)
2. `.agentic-ci/openshell-policy.yml` in the workdir (repo-level convention)
3. Built-in default policy (see below)

### Default Policy

Allows common CI/CD endpoints, blocks everything else:

```yaml
network_policies:
  vertex_ai:
    endpoints:
      - host: "*.googleapis.com"
        port: 443
        tls: terminate
        enforcement: enforce
    binaries:
      - path: "*"

  github:
    endpoints:
      - host: "*.github.com"
        port: 443
        tls: terminate
        enforcement: enforce
    binaries:
      - path: "*"

  gitlab_api:
    endpoints:
      - host: "*.gitlab.com"
        port: 443
        tls: terminate
        enforcement: enforce
    binaries:
      - path: "*"

  pypi:
    endpoints:
      - host: pypi.org
        port: 443
        tls: terminate
        enforcement: enforce
      - host: files.pythonhosted.org
        port: 443
        tls: terminate
        enforcement: enforce
    binaries:
      - path: "*"
```

Teams with different needs override via `.agentic-ci/openshell-policy.yml` in their repo.

## Module Architecture

```
src/agentic_ci/
    cli.py          # argparse entry point, dispatches to setup/run
    gateway.py      # start embedded gateway, check if running
    sandbox.py      # create sandbox, check if exists, exec commands
    policy.py       # policy resolution chain + built-in default
    claude.py       # build Claude command, manage execution in sandbox
    stream.py       # stream-json parser, pretty-printing, token rate
    otel.py         # OTEL collector + summary (merged)
```

### cli.py

Argparse with two subcommands. `setup` calls `gateway.start()` then `sandbox.create()`. `run` checks status via `gateway.is_running()` and `sandbox.exists()`, optionally calls setup, then orchestrates claude/stream/otel.

### gateway.py

Thin wrapper over `openshell` CLI for gateway lifecycle:

- `is_running()` — checks `openshell status` to determine if a gateway is active.
- `start()` — runs `openshell gateway start` to launch an embedded gateway.

### sandbox.py

Thin wrapper over `openshell` CLI for sandbox lifecycle:

- `exists()` — checks `openshell status` to determine if a sandbox is already created.
- `create(policy_path)` — creates a sandbox with Claude as the agent and applies the resolved policy.
- `exec(cmd)` — runs a command inside the sandbox via `openshell sandbox exec`.

### policy.py

Implements the policy resolution chain:

- `resolve(flag_path, workdir)` — returns the path to the policy file to use, following the priority order (flag > repo file > default).
- The built-in default policy is an embedded YAML string, written to a temp file when needed.

### claude.py

Builds the Claude CLI invocation and manages execution:

- Constructs the command with model, prompt, and extra args.
- Sets up OTEL environment variables (`CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_EXPORTER_OTLP_ENDPOINT`, etc.) pointing to the local OTEL collector port.
- Calls `sandbox.exec()` with the constructed command.

### stream.py

Existing `StreamProcessor` class, largely unchanged from current codebase:

- Parses Claude's stream-json event types (content_block_start/delta/stop, message_start/delta, errors).
- Pretty-prints text, thinking blocks, and tool calls with colored ANSI output.
- Displays token rate from OTEL rate file.
- Word-wrapping support.

### otel.py

Merges current `otel_collector.py` and `otel_summary.py`:

- Lightweight OTLP HTTP/JSON receiver on a dynamic port (Python stdlib `http.server`).
- Appends payloads to JSONL log file.
- Tracks token usage in a sliding 60-second window, writes rate file.
- `print_summary()` parses the JSONL log and prints token counts, cost, active time, API request stats.

## Runtime Flow

```
1. cli.py parses args
       │
2. gateway.is_running()?  ──no──►  gateway.start()
       │yes                              │
       ▼                                 │
3. sandbox.exists()?  ────no──►  policy.resolve(flag, workdir)
       │yes                        │
       ▼                           ▼
       │                     sandbox.create(policy_path)
       │◄──────────────────────────┘
       │
4. otel.start_collector()  →  binds dynamic port
       │
5. claude.build_command(prompt, model, otel_port, extra_args)
       │
6. sandbox.exec(claude_cmd)
       │
       ├──► stdout piped to stream.StreamProcessor
       │         └──► pretty-prints to terminal
       │         └──► reads OTEL rate file for token/sec display
       │
       └──► Claude sends metrics to OTEL collector (localhost:port)
                └──► otel writes to JSONL log
       │
7. Claude exits
       │
8. otel.print_summary()  →  token counts, cost, active time
       │
9. Copy artifacts to CI workspace (if GITHUB_WORKSPACE or CI_PROJECT_DIR set)
```

- Steps 2-3 are the auto-setup path — only runs if `openshell status` shows things aren't ready.
- The OTEL collector runs inside the CI container (not in the sandbox). Claude inside the sandbox sends metrics to it via OTEL exporter env vars. The endpoint must be reachable from inside the sandbox — OpenShell's network namespace uses a veth pair (`10.200.0.1` on the host side), so the OTEL endpoint should be set to `http://10.200.0.1:<port>`. This needs validation during implementation.
- Stream processing happens in real-time as Claude produces output.

## Migration: What Changes

| Current module | Fate | Notes |
|---|---|---|
| `cli.py` | **Rewrite** | New argparse with `setup` and `run` subcommands |
| `runner.py` | **Delete** | Orchestration splits into `cli.py` and `claude.py` |
| `stream.py` | **Keep** | Largely unchanged |
| `otel_collector.py` | **Merge → `otel.py`** | Collector + summary in one module |
| `otel_summary.py` | **Merge → `otel.py`** | See above |
| `extract.py` | **Delete** | Use-case-specific, not carrying forward |
| `setup.py` | **Delete** | Container bootstrap replaced by base image + OpenShell |
| — | **New: `gateway.py`** | OpenShell gateway lifecycle |
| — | **New: `sandbox.py`** | OpenShell sandbox lifecycle + exec |
| — | **New: `policy.py`** | Policy resolution + built-in default |
| — | **New: `claude.py`** | Claude command building + OTEL env setup |

## Integration with OpenShell

agentic-ci shells out to the `openshell` CLI for all OpenShell interactions. The Python SDK was considered but cannot start gateways (CLI-only). Key commands used:

- `openshell status` — check if gateway/sandbox are running
- `openshell gateway start` — start embedded gateway
- `openshell sandbox create -- claude` — create sandbox with Claude agent
- `openshell sandbox exec <name> -- <cmd>` — execute command in sandbox
- `openshell policy set <name> --policy <file>` — apply policy

## Usage Examples

### Simple: single prompt

```bash
agentic-ci run "Fix the flaky test in test_auth.py"
```

Auto-starts gateway, creates sandbox with default policy, runs Claude, streams output, prints summary.

### Multi-prompt workflow

```yaml
steps:
  - run: agentic-ci setup
  - run: agentic-ci run "Fix the flaky test in test_auth.py"
  - run: agentic-ci run "Update the changelog"
```

### Custom policy

```bash
agentic-ci run --policy my-policy.yml "Deploy the staging environment"
```

### Repo-level policy (convention)

```
my-repo/
  .agentic-ci/
    openshell-policy.yml    # auto-discovered
  src/
  tests/
```

```bash
agentic-ci run "Add input validation to the API"
# Automatically uses .agentic-ci/openshell-policy.yml
```
