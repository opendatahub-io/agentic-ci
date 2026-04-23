# agentic-ci

Tooling for running AI coding agents in CI/CD environments. Handles
container setup, telemetry collection, streaming output, and process
lifecycle so your CI workflows don't have to.

## Install

```bash
pip install ./agentic-ci/
```

## Commands

### `agentic-ci setup`

Bootstrap a CI container for running Claude Code. Installs system
dependencies, creates a non-root user (`claude-ci`), and installs
Claude Code.

```bash
WORKSPACE_DIR=/workspace agentic-ci setup
```

### `agentic-ci run`

Run Claude Code with telemetry and streaming output. Starts an OTEL
collector, runs Claude with stream-json output, displays
human-readable progress, and prints a token/cost summary.

When run as root, automatically re-execs as `claude-ci`.

```bash
agentic-ci run "your prompt here" /path/to/workdir
agentic-ci run "prompt" . --model claude-sonnet-4-6
```

### `agentic-ci stream`

Parse Claude Code stream-json from stdin into human-readable CI logs
with token tracking.

```bash
claude -p "prompt" --output-format stream-json | agentic-ci stream
```

### `agentic-ci otel-collect`

Start a lightweight OTLP HTTP/JSON receiver for capturing token and
cost metrics. Binds to an OS-assigned port and writes it to
`$OTEL_PORT_FILE` for discovery.

### `agentic-ci otel-summary`

Print a human-readable token/cost summary from an OTEL JSONL log.

```bash
agentic-ci otel-summary /path/to/claude-otel.jsonl
```

### `agentic-ci extract`

Extract a structured JSON result from Claude's stream-json output.

```bash
agentic-ci extract stream-capture.jsonl output-dir/
```

## Environment Variables

| Variable | Default | Used by |
|---|---|---|
| `WORKSPACE_DIR` | `/workspace` | `setup`, `run` |
| `CLAUDE_MODEL` | `claude-opus-4-6` | `run` |
| `OTEL_LOG_FILE` | `/tmp/claude-otel.jsonl` | `otel-collect` |
| `OTEL_RATE_FILE` | `/tmp/claude-otel-rate.json` | `otel-collect`, `stream` |
| `OTEL_COLLECTOR_PORT` | `4318` | `otel-collect` |
| `OTEL_PORT_FILE` | — | `otel-collect` |
| `STREAM_CAPTURE_FILE` | `<run_tmp>/claude-stream-capture.jsonl` | `run` |

## Python API

All commands are also importable:

```python
from agentic_ci.runner import run
from agentic_ci.stream import StreamProcessor
from agentic_ci.otel_summary import print_summary
```
