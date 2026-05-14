# CLAUDE.md

## Introduction

agentic-ci runs Claude Code in sandboxed CI environments with pluggable backends. Users run `agentic-ci run "prompt"` to execute Claude in an isolated environment with streaming output and OTEL telemetry.

Two backends are available:
- **Podman** (default): Runs Claude in a Podman container. Simple, widely available.
- **OpenShell**: Runs Claude in an [OpenShell](https://github.com/NVIDIA/OpenShell) sandbox with network policy enforcement and filesystem isolation.

Zero external Python dependencies (stdlib only).

## Architecture

```
src/agentic_ci/
    cli.py              # Entry point, backend selection, OTEL orchestration
    backend.py          # Backend ABC + shared stream processing
    backends/
        __init__.py     # Backend factory (create_backend)
        podman.py       # PodmanBackend — container execution
        openshell/
            __init__.py # OpenShellBackend — sandbox execution
            gateway.py  # OpenShell gateway lifecycle
            sandbox.py  # OpenShell sandbox lifecycle
            policy.py   # Policy resolution + built-in default
    stream.py           # Stream-json parser, pretty-printing
    otel.py             # OTLP collector + token/cost summary
```

- **`cli.py`**: Argparse entry point with `setup` and `run` subcommands plus `--backend` flag. Delegates to backend for setup/execution, handles OTEL lifecycle.

- **`backend.py`**: Abstract `Backend` class with `setup()` and `run()` methods. Shared `_process_stream()` helper reads stream-json from a subprocess through `StreamProcessor`.

- **`backends/podman.py`**: `PodmanBackend` — runs Claude in a `podman run --rm` container. Mounts workdir and gcloud credentials as volumes. Uses `--network host` when OTEL is enabled.

- **`backends/openshell/`**: `OpenShellBackend` — runs Claude in an OpenShell sandbox. Manages gateway lifecycle, sandbox creation with network policy, and credential upload. Submodules: `gateway.py`, `sandbox.py`, `policy.py`.

- **`stream.py`**: Parses Claude's stream-json output into human-readable CI logs with colored ANSI output, tool call summaries, and token rate display.

- **`otel.py`**: Lightweight OTLP HTTP/JSON receiver (stdlib `http.server`) that logs payloads to JSONL, tracks token usage over a sliding window, and prints a token/cost summary.

### Key

- **Claude Code only** for now. Other agents can be added later.
- **Vertex AI** for Claude API access. Credentials are staged via gcloud ADC files.
- **OTEL collector runs on the host**, not inside the sandbox/container.

## Commands

```bash
tox -e py313                     # run tests
tox -e lint                      # ruff lint
tox -e lint-fix                  # ruff lint with auto-fix
tox -e check-format              # ruff format check
tox -e format                    # ruff format with auto-fix
tox -e typecheck                 # mypy type check
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

- Python 3.10+, uv for local dev. Zero runtime dependencies (stdlib only).
- `ruff` for lint and format. Config in `pyproject.toml`. `tox` orchestrates all checks.
- Fix lint errors at the source. Don't suppress with `# noqa` or exclude files from linting.
- All tests live under `tests/`.
- `pytest` for tests.
