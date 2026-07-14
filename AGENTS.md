# Agentic CI

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines,
PR requirements, and harness/backend parity rules.

agentic-ci runs AI coding agents in sandboxed CI environments with pluggable backends and harnesses. Users run `agentic-ci run "prompt"` to execute an agent in an isolated environment with streaming output and OTEL telemetry.

Three **backends** provide execution environments:
- **Local**: Runs the agent directly in the current environment. No container or sandbox — the agent binary must be on PATH. Useful inside existing CI containers (e.g. Prow step images).
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
        local.py        # LocalBackend — direct execution (no container)
        podman.py       # PodmanBackend — container execution
        openshell/
            __init__.py # OpenShellBackend — sandbox execution
            gateway.py  # OpenShell gateway lifecycle
            sandbox.py  # OpenShell sandbox lifecycle
            policy.py   # Policy resolution + built-in default
    plugins.py          # Plugin/skill install (build-time) and filtering (runtime)
    stream.py           # Stream parsers for Claude Code and OpenCode output
    otel.py             # OTLP collector + token/cost summary
```

- **`cli.py`**: Argparse entry point with `setup`, `run`, and `stop` subcommands plus `--backend` and `--harness` flags. Creates harness and backend, handles OTEL lifecycle.

- **`backend.py`**: Abstract `Backend` class with `setup()` and `run()` methods. Shared `_process_stream()` helper reads output from a subprocess through the harness's stream processor.

- **`harness.py`**: Abstract `Harness` class encapsulating agent-specific CLI args, env vars, credential paths, and stream parsing. Implementations: `ClaudeCodeHarness`, `OpenCodeHarness`.

- **`plugins.py`**: Build-time plugin installation (`install_claude_plugins`, `install_opencode_skills`) and runtime filtering (`enable_plugins`). At build time, installs skills from the skills-registry marketplace into the container image and writes a plugin-to-skill manifest. At runtime, `AGENT_ENABLED_PLUGINS` controls which plugins are active: Claude Code disables plugins in `settings.json`; OpenCode deletes unwanted skill directories from disk (since `permission.skill.deny` doesn't prevent loading with `--dangerously-skip-permissions`).

- **`backends/podman.py`**: `PodmanBackend` — runs the agent in a `podman run` container. Bind-mounts the workdir into the container at `/workspace`, so changes are visible on the host immediately. Mounts gcloud credentials as read-only volumes. Uses `--network host` when OTEL is enabled.

- **`backends/openshell/`**: `OpenShellBackend` — runs the agent in an OpenShell sandbox. Uploads the workdir into the sandbox on `setup()` and downloads it back after `run()` completes. Only changes inside the workdir are reflected back to the host; files written elsewhere in the sandbox are not retrieved. Manages gateway lifecycle, sandbox creation with network policy, and credential injection. Submodules: `gateway.py`, `sandbox.py`, `policy.py`.

- **`stream.py`**: `ClaudeCodeStreamProcessor` parses Claude Code's `stream-json` output. `OpenCodeStreamProcessor` parses OpenCode's JSON event output. Both produce human-readable CI logs with colored ANSI output, tool call summaries, and token display.

- **`otel.py`**: Lightweight OTLP HTTP/JSON receiver (stdlib `http.server`) that logs payloads to JSONL, tracks token usage over a sliding window, and prints a token/cost summary.

### Key

- **Authentication** is auto-detected: if `ANTHROPIC_API_KEY` is set, direct API auth is used (no gcloud credentials needed); otherwise Vertex AI with gcloud ADC files.
- **OTEL collector runs on the host**, not inside the sandbox/container. Currently only Claude Code emits OTEL metrics; OpenCode provides token/cost data via its JSON output.

## Container images

Pre-built container images for running AI coding agents in CI. Published
to `quay.io/aipcc/agentic-ci/`.

```text
images/
  runner/
    shared/
      Containerfile.base            — Runner base image (UBI10 + common tools)
      Containerfile.openshell-base  — OpenShell sandbox base image
      entrypoint.sh                 — Container entrypoint (credential setup + exec)
    claude-code/
      Containerfile                 — Claude Code runner image
      Containerfile.openshell       — Claude Code sandbox image (OpenShell)
    opencode/
      Containerfile                 — OpenCode runner image
      Containerfile.openshell       — OpenCode sandbox image (OpenShell)
      opencode.json                 — Seed config for CI headless mode
  ci/
    Containerfile.podman            — CI environment image (podman + tools)
    Containerfile.openshell         — CI environment image (OpenShell + podman)
  openshell-supervisor/
    Containerfile                   — OpenShell supervisor (built from source)
scripts/
  bump-versions.py                  — Bump pinned dependency versions in Containerfiles
```

The runner-base Containerfile (`images/runner/shared/Containerfile.base`)
is pre-built as `localhost/base:latest` before building the Claude and
OpenCode runner images. It is NOT published to any registry as a
standalone image. Do not add a CI job to push runner-base separately.

### Building locally

```bash
make base-build              # build runner base image locally
make claude-build            # build Claude Code runner image (includes base)
make opencode-build          # build OpenCode runner image (includes base)
make ci-build                # build CI podman image
make openshell-base-build    # build OpenShell sandbox base image
make openshell-claude-build  # build Claude sandbox (includes openshell-base)
make openshell-opencode-build # build OpenCode sandbox (includes openshell-base)
make openshell-supervisor-build # build OpenShell supervisor image
make openshell-ci-build      # build OpenShell CI image
make image-lint              # shellcheck + ruff on image scripts
make image-test              # run image unit tests
```

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

## Mergify

`.mergify.yml` defines merge protection rules with required CI checks. When adding, removing, or renaming jobs in `.github/workflows/`, update `.mergify.yml` to match. The file patterns in Mergify rules must stay aligned with the `paths:` filters in each workflow.

## Conventions

- Python 3.10+, uv for local dev. Minimal runtime dependencies (`requests`, `tenacity`).
- `ruff` for lint and format. Config in `pyproject.toml`. `tox` orchestrates all checks.
- Always place imports at the top of the file. No function-level or inline imports.
- Fix lint errors at the source. Don't suppress with `# noqa` or exclude files from linting.
- All tests live under `tests/`.
- `pytest` for tests.

## Debugging

Use the `/debug-agentic-ci` skill when investigating infrastructure-level failures. It provides a symptom catalog and structured RCA template.

When fixing a bug or adding a feature that changes failure modes, update `.claude/skills/debug-agentic-ci/references/symptoms.md` with the new pattern so future investigations have it in context.

When investigating this repo specifically, focus on these areas by symptom:

- **Container failed**: Check `backends/podman.py` or `backends/openshell/` for container launch logic. Check `harness.py` for agent CLI argument construction. Check `cli.py` for credential and OTEL setup. Check `stream.py` if output parsing failed.
- **Skills not found / wrong skills loaded**: Check `plugins.py` for install-time skill discovery (`install_opencode_skills` fallback dirs, manifest generation) and runtime filtering (`enable_plugins` reads `AGENT_ENABLED_PLUGINS`). Check `harness.py` `build_env_args()` and `build_env_script_lines()` for env var forwarding to the container. For OpenCode, filtering deletes unwanted skill directories from disk; for Claude Code, it disables plugins in `settings.json`.
- **Skill engine failure**: Check `skill.py` for the `run_skill()` flow: pre-gates, container launch, post-gates, verdict loading. Check which phase returned an error.
- **MR/PR operations failed**: Check `forge.py` and the `forge` CLI subcommands. Check `git.py` for clone/push/branch operations. Check error handling in `ForgeError`.
- **Gate framework issues**: Check `gates.py` for the gate registry and execution order. Check if a gate was added or changed that altered behavior. Gates run as pre/post hooks around the agent; the wiring is in the calling repo (autofix), but the gate implementations may be here.
