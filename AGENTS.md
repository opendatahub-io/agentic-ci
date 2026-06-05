# Agentic CI

agentic-ci runs AI coding agents in sandboxed CI environments with pluggable backends and harnesses. Users run `agentic-ci run "prompt"` to execute an agent in an isolated environment with streaming output and OTEL telemetry.

Two **backends** provide execution environments:
- **Podman** (default): Runs the agent in a Podman container. Simple, widely available.
- **OpenShell**: Runs the agent in an [OpenShell](https://github.com/NVIDIA/OpenShell) sandbox with network policy enforcement and filesystem isolation.

Two **harnesses** define which agent CLI to run:
- **claude-code** (default): [Claude Code](https://docs.anthropic.com/en/docs/claude-code) with `stream-json` output format.
- **opencode**: [OpenCode](https://github.com/anomalyco/opencode) with JSON event output format.

## Architecture

```text
src/agentic_ci/
    cli.py              # Entry point, backend/harness selection, OTEL orchestration
    backend.py          # Backend ABC + shared stream processing
    harness.py          # Harness ABC + ClaudeCode/OpenCode implementations
    backends/
        __init__.py     # Backend factory (create_backend)
        podman.py       # PodmanBackend — container execution
        openshell/
            __init__.py # OpenShellBackend — sandbox execution
            gateway.py  # OpenShell gateway lifecycle
            sandbox.py  # OpenShell sandbox lifecycle
            policy.py   # Policy resolution + built-in default
    stream.py           # Stream parsers for Claude Code and OpenCode output
    otel.py             # OTLP collector + token/cost summary
```

- **`cli.py`**: Argparse entry point with `setup`, `run`, and `stop` subcommands plus `--backend` and `--harness` flags. Creates harness and backend, handles OTEL lifecycle.

- **`backend.py`**: Abstract `Backend` class with `setup()` and `run()` methods. Shared `_process_stream()` helper reads output from a subprocess through the harness's stream processor.

- **`harness.py`**: Abstract `Harness` class encapsulating agent-specific CLI args, env vars, credential paths, and stream parsing. Implementations: `ClaudeCodeHarness`, `OpenCodeHarness`.

- **`backends/podman.py`**: `PodmanBackend` — runs the agent in a `podman run` container. Mounts workdir and gcloud credentials as volumes. Uses `--network host` when OTEL is enabled.

- **`backends/openshell/`**: `OpenShellBackend` — runs the agent in an OpenShell sandbox. Manages gateway lifecycle, sandbox creation with network policy, and credential upload. Submodules: `gateway.py`, `sandbox.py`, `policy.py`.

- **`stream.py`**: `ClaudeCodeStreamProcessor` parses Claude Code's `stream-json` output. `OpenCodeStreamProcessor` parses OpenCode's JSON event output. Both produce human-readable CI logs with colored ANSI output, tool call summaries, and token display.

- **`otel.py`**: Lightweight OTLP HTTP/JSON receiver (stdlib `http.server`) that logs payloads to JSONL, tracks token usage over a sliding window, and prints a token/cost summary.

### Key

- **Authentication** is auto-detected: if `ANTHROPIC_API_KEY` is set, direct API auth is used (no gcloud credentials needed); otherwise Vertex AI with gcloud ADC files.
- **OTEL collector runs on the host**, not inside the sandbox/container. Currently only Claude Code emits OTEL metrics; OpenCode provides token/cost data via its JSON output.

## Commands

```bash
tox -e py313                     # run tests
tox -e lint                      # ruff lint
tox -e lint-fix                  # ruff lint with auto-fix
tox -e check-format              # ruff format check
tox -e format                    # ruff format with auto-fix
tox -e typecheck                 # mypy type check
tox -e docs                      # build API docs
```

## Verification

After every code change, run all four checks before reporting the task as done:

```bash
tox -e py313                     # tests
tox -e lint                      # ruff lint
tox -e check-format              # ruff format check
tox -e typecheck                 # mypy type check
```

Fix any failures before moving on. Do not skip any of these checks.

## Conventions

- Python 3.10+, uv for local dev. Minimal runtime dependencies (`requests`, `tenacity`).
- `ruff` for lint and format. Config in `pyproject.toml`. `tox` orchestrates all checks.
- Fix lint errors at the source. Don't suppress with `# noqa` or exclude files from linting.
- All tests live under `tests/`.
- `pytest` for tests.
