# Agentic CI

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines,
PR requirements, and harness/backend parity rules.

agentic-ci runs AI coding agents in sandboxed CI environments with pluggable backends and harnesses. Users run `agentic-ci run "prompt"` to execute an agent in an isolated environment with streaming output and OTEL telemetry.

Three **backends** provide execution environments:
- **Local**: Runs the agent directly in the current environment. No container or sandbox — the agent binary must be on PATH. Useful inside existing CI containers (e.g. Prow step images).
- **Podman** (default): Runs the agent in a Podman container. Simple, widely available.
- **OpenShell**: Runs the agent in an [OpenShell](https://github.com/NVIDIA/OpenShell) sandbox with network policy enforcement and filesystem isolation.

Three **harnesses** define which agent CLI to run:
- **claude-code** (default): [Claude Code](https://docs.anthropic.com/en/docs/claude-code) with `stream-json` output format.
- **opencode**: [OpenCode](https://github.com/anomalyco/opencode) with JSON event output format.
- **codex**: [OpenAI Codex](https://developers.openai.com/codex/cli) with JSONL event output and native OpenTelemetry export.

## Architecture

```text
src/agentic_ci/
    cli.py              # Entry point, backend/harness selection, OTEL orchestration
    backend.py          # Backend ABC + shared stream processing
    harness.py          # Harness ABC + ClaudeCode/OpenCode/Codex implementations
    backends/
        __init__.py     # Backend factory (create_backend)
        local.py        # LocalBackend — direct execution (no container)
        podman.py       # PodmanBackend — container execution
        openshell/
            __init__.py # OpenShellBackend — sandbox execution
            gateway.py  # OpenShell gateway lifecycle
            sandbox.py  # OpenShell sandbox lifecycle
            policy.py   # Policy resolution + built-in default
    config.py           # Project config loader (.agentic-ci/config.yml)
    plugins.py          # Plugin/skill install (build-time) and filtering (runtime)
    stream.py           # Stream parsers for Claude Code, OpenCode, and Codex output
    otel.py             # OTLP collector + token/cost summary
```

- **`cli.py`**: Argparse entry point with `setup`, `run`, and `stop` subcommands plus `--backend` and `--harness` flags. Creates harness and backend, handles OTEL lifecycle.

- **`backend.py`**: Abstract `Backend` class with `setup()` and `run()` methods. Shared `_process_stream()` helper reads output from a subprocess through the harness's stream processor.

- **`harness.py`**: Abstract `Harness` class encapsulating agent-specific CLI args, env vars, credential paths, and stream parsing. Implementations: `ClaudeCodeHarness`, `OpenCodeHarness`, `CodexHarness`.

- **`plugins.py`**: Build-time plugin installation (`install_claude_plugins`, `install_opencode_skills`, `install_codex_plugins`) and runtime filtering (`enable_plugins`). At build time, installs plugins or skills from the skills-registry marketplace into the container image and writes a plugin-to-skill manifest. At runtime, `AGENT_ENABLED_PLUGINS` controls which plugins are active: Claude Code disables plugins in `settings.json`; OpenCode deletes unwanted skill directories from disk; Codex removes unwanted native plugins and manifest-managed compatibility skills while preserving unmanaged personal skills.

- **`backends/podman.py`**: `PodmanBackend` — runs the agent in a `podman run` container. Bind-mounts the workdir into the container at `/workspace`, so changes are visible on the host immediately. Mounts gcloud credentials as read-only volumes. Uses `--network host` when OTEL is enabled.

- **`config.py`**: Loads project configuration from `.agentic-ci/config.yml` in the workdir. Currently supports a `setup` key with a list of commands (bare strings or `{name, run}` objects) that run on the host before sandbox upload, enabling dependency installation for repos whose agents need it.

- **`backends/openshell/`**: `OpenShellBackend` — runs the agent in an OpenShell sandbox. Uploads the workdir into the sandbox on `setup()` and downloads it back after `run()` completes. Only changes inside the workdir are reflected back to the host; files written elsewhere in the sandbox are not retrieved. Manages gateway lifecycle, sandbox creation with network policy, credential injection, and setup steps. Submodules: `gateway.py`, `sandbox.py`, `policy.py`.

- **`stream.py`**: `ClaudeCodeStreamProcessor` parses Claude Code's `stream-json` output. `OpenCodeStreamProcessor` parses OpenCode's JSON event output. `CodexStreamProcessor` parses Codex JSONL events. All produce human-readable CI logs with colored ANSI output, tool call summaries, and token display.

- **`otel.py`**: Lightweight OTLP HTTP/JSON receiver (stdlib `http.server`) that logs payloads to JSONL, tracks token usage over a sliding window, and prints a token/cost summary.

### Key

- **Authentication** is harness-specific: Claude Code uses `ANTHROPIC_API_KEY` when set and otherwise Vertex AI with gcloud ADC files; Codex uses `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `OPENAI_API_KEY`, or local `$CODEX_HOME/auth.json` login state.
- **OTEL collector runs on the host**, not inside the sandbox/container. Claude Code and Codex export OTEL data; OpenCode provides token/cost data via its JSON output.

## Container images

Pre-built container images for running AI coding agents in CI. Published
to `quay.io/aipcc/agentic-ci/`.

```text
images/
  runner/
    shared/
      Containerfile.base            — Runner base image (UBI10 + common tools)
      entrypoint.sh                 — Container entrypoint (credential setup + exec)
    claude-code/
      Containerfile                 — Claude Code runner image
      Containerfile.openshell       — Claude Code sandbox image (OpenShell); builds
                                      on the agentic base image
    opencode/
      Containerfile                 — OpenCode runner image
      Containerfile.openshell       — OpenCode sandbox image (OpenShell); builds
                                      on the agentic base image
      opencode.json                 — Seed config for CI headless mode
  ci/
    Containerfile.podman            — CI environment image (podman + tools, UBI10)
    Containerfile.openshell         — CI environment image (OpenShell + podman, UBI9)
scripts/
  bump-versions.py                  — Bump pinned dependency versions in Containerfiles
```

OpenShell is consumed from UBI9 artifacts: the CLI binary copied from
`quay.io/opendatahub/odh-openshell-cli`, the gateway binary copied from
`quay.io/opendatahub/odh-openshell-gateway`, and the supervisor pulled at
runtime from `quay.io/opendatahub/odh-openshell-supervisor`. The
OpenShell-path sandbox images (`Containerfile.openshell`) are UBI9; the
podman-path images stay on UBI10.

Both sandbox images build `FROM` their respective agentic base images
(`quay.io/aipcc/base-images/agentic/claude-code` and
`quay.io/aipcc/base-images/agentic/opencode`, pinned by digest). These
base images ship the agent binary and Node on UBI9; the Containerfiles
layer the agentic-ci tooling (Python, uv, forge CLIs, agentic-ci, skills)
on top.

The runner-base Containerfile (`images/runner/shared/Containerfile.base`)
is pre-built as `localhost/base:latest` before building the Claude and
OpenCode runner images. It is NOT published to any registry as a
standalone image. Do not add a CI job to push runner-base separately.

### Building locally

The sandbox images pull private base images from `quay.io/aipcc/base-images/`.
Log in before building them: `podman login quay.io`

```bash
make base-build              # build runner base image locally
make claude-build            # build Claude Code runner image (includes base)
make opencode-build          # build OpenCode runner image (includes base)
make ci-build                # build CI podman image
make openshell-claude-build  # build Claude sandbox (builds on the agentic base image)
make openshell-opencode-build # build OpenCode sandbox (builds on the agentic base image)
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
- **Skills not found / wrong skills loaded**: Check `plugins.py` for install-time skill discovery (`install_opencode_skills` fallback dirs, `install_codex_plugins` native/compatibility paths, manifest generation) and runtime filtering (`enable_plugins` reads `AGENT_ENABLED_PLUGINS`). Check `harness.py` `build_env_args()` and `build_env_script_lines()` for env var forwarding to the container. Claude Code disables unwanted plugins in `settings.json`; OpenCode deletes unwanted skill directories; Codex removes unwanted native plugins and only manifest-managed compatibility skills.
- **Skill engine failure**: Check `skill.py` for the `run_skill()` flow: pre-gates, container launch, post-gates, verdict loading. Check which phase returned an error.
- **MR/PR operations failed**: Check `forge.py` and the `forge` CLI subcommands. Check `git.py` for clone/push/branch operations. Check error handling in `ForgeError`.
- **Gate framework issues**: Check `gates.py` for the gate registry and execution order. Check if a gate was added or changed that altered behavior. Gates run as pre/post hooks around the agent; the wiring is in the calling repo (autofix), but the gate implementations may be here.
