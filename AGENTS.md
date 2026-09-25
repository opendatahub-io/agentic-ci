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
    sandbox_profile.py  # Sandbox profile schema, parsing, merge and hashing
    plugins.py          # Plugin/skill install (build-time) and filtering (runtime)
    stream.py           # Stream parsers for Claude Code, OpenCode, and Codex output
    telemetry.py        # Generic event transport for the OTLP trace pipeline
    routing.py          # Difficulty classifier + model tier routing (run_routed_skill)
    models.py           # Model registry: default model, routing tiers, effort levels per harness
    otel.py             # OTLP collector + token/cost summary
```

- **`cli.py`**: Argparse entry point with `setup`, `run`, and `stop` subcommands plus `--backend` and `--harness` flags. Creates harness and backend, handles OTEL lifecycle.

- **`backend.py`**: Abstract `Backend` class with `setup()` and `run()` methods. Shared `_process_stream()` helper reads output from a subprocess through the harness's stream processor. When `output_file` is set on the backend, `_process_stream()` tees decoded stdout lines to disk until the stream processor reports completion. `_snapshot_host_git()` / `_restore_host_git()` record the workdir's git control files (`.git/config`, `config.worktree`, `commondir`, `hooks/`, `info/`) before a sandboxed agent can write them and put them back afterwards, so host-side git never runs agent-configured hooks, fsmonitor or drivers. Every backend that lets the agent write the host workdir (Podman, OpenShell) must call both.

- **`harness.py`**: Abstract `Harness` class encapsulating agent-specific CLI args, env vars, credential paths, and stream parsing. Implementations: `ClaudeCodeHarness`, `OpenCodeHarness`, `CodexHarness`. Model ids and effort levels are not defined here; each harness reads them from `models.py` via its `registry_key`.

- **`models.py`**: `MODEL_REGISTRY`, the single map of default model, default reasoning effort, `low`/`medium`/`high` routing tiers, accepted reasoning-effort values, sub-agent effort, and classifier effort per harness. Harnesses only know how to turn an effort value into a CLI flag (`effort_args()`); `Harness.resolve_efforts()` applies the `--effort` flag, then the `*_REASONING_EFFORT` env var, then the registry default.

- **`routing.py`**: Difficulty-based routing for `run_routed_skill()`: classifier prompt, `route.json` parsing, tier resolution, and `RouteDecision`. Pure module; the skill engine supplies the agent invocation.

- **`plugins.py`**: Build-time plugin installation (`install_claude_plugins`, `install_opencode_skills`, `install_codex_plugins`) and runtime filtering (`enable_plugins`). At build time, installs plugins or skills from the skills-registry marketplace (supporting both legacy `repo` and `git-subdir` source formats) into the container image and writes a plugin-to-skill manifest. All marketplace source paths are validated against the clone root to prevent directory traversal. At runtime, `AGENT_ENABLED_PLUGINS` controls which plugins are active: Claude Code disables plugins in `settings.json`; OpenCode deletes unwanted skill directories from disk; Codex removes unwanted native plugins and manifest-managed compatibility skills while preserving unmanaged personal skills.

- **`sandbox_profile.py`**: Frozen `SandboxProfile` dataclasses describing what a target repo needs in the sandbox (toolchains, egress presets, setup, validate, skips, env, resources, `discard_before_download`, `overlay`). `parse_profile(data, source="central"|"overlay")` validates raw JSON/YAML (overlay problems that could widen access are dropped with warnings), `merge_profiles()` applies central-wins precedence, and `profile_to_dict()` / `profile_hash()` serialize. `SkillConfig.sandbox_profile` carries it through `run_skill` / `run_routed_skill` and `_AgentSession` to `create_backend`, which passes it only to `OpenShellBackend` and only when set; other backends warn and ignore it. Only `resources` takes effect so far (explicit backend kwargs win).

- **`backends/podman.py`**: `PodmanBackend` — runs the agent in a `podman run` container. Bind-mounts the workdir into the container at `/workspace`, so changes are visible on the host immediately. Mounts gcloud credentials as read-only volumes. Uses `--network host` when OTEL is enabled.

- **`config.py`**: Loads project configuration from `.agentic-ci/config.yml` in the workdir. Currently supports a `setup` key with a list of commands (bare strings or `{name, run}` objects) that run on the host before sandbox upload, enabling dependency installation for repos whose agents need it.

- **`backends/openshell/`**: `OpenShellBackend` — runs the agent in an OpenShell sandbox. Uploads the workdir into the sandbox on `setup()` and downloads it back after `run()` completes. Only changes inside the workdir are reflected back to the host; files written elsewhere in the sandbox are not retrieved. Manages gateway lifecycle, sandbox creation with network policy, credential injection, and setup steps. Network policies are scoped by harness authentication mode (`vertex`, `api-key`, `oauth`, `openai`); the backend detects mode changes between runs and recreates the sandbox when modes differ. `oauth` (a Claude subscription token) creates no provider, so its mode is recorded only in the sandbox identity file. Submodules: `gateway.py`, `sandbox.py`, `policy.py`.

- **`stream.py`**: `ClaudeCodeStreamProcessor` parses Claude Code's `stream-json` output. `OpenCodeStreamProcessor` parses OpenCode's JSON event output. `CodexStreamProcessor` parses Codex JSONL events. All produce human-readable CI logs with colored ANSI output, tool call summaries, and token display.

- **`telemetry.py`**: Generic event transport for the existing OTLP trace pipeline. Owns transport and serialization only — producers own event schemas, workflow timing, and outcome classification. Validates events for privacy (rejects sensitive keys, emails, bearer tokens, private keys) and size limits. Emits events as zero-duration OTLP spans so existing JSONL and MLflow trace exporters consume them without separate storage. File paths are constrained to a runner-owned log root with symlink and traversal protection. Raises transport errors; callers decide whether export failure is fatal.

- **`otel.py`**: Lightweight OTLP HTTP/JSON receiver (stdlib `http.server`) that logs payloads to JSONL, tracks token usage over a sliding window, and prints a token/cost summary.

### Key

- **Reasoning effort** is passed on every run (`claude --effort`, `opencode --variant`, `codex -c model_reasoning_effort=` plus `agents.default_subagent_reasoning_effort`), default `high` from `models.py`; overrides via `--effort` or `CLAUDE_REASONING_EFFORT` / `OPENCODE_REASONING_EFFORT` / `CODEX_REASONING_EFFORT` / `CODEX_SUBAGENT_REASONING_EFFORT`. Callers pass the resolved effort to `Backend.run(effort=...)` alongside the flags in `extra_args`, and every backend exports it to the agent as `AGENT_REASONING_EFFORT` next to `AGENT_MODEL` (output only, never read back).
- **Nested Codex runs**: `-m` and `-c` flags reach only the top-level `codex exec`. On OpenShell (`externally_sandboxed=True`), `CodexHarness.build_args()` also writes the model, update check, efforts, and OTLP exporters (`run_settings_toml()`) into a marked block at the top of the sandbox's `$CODEX_HOME/config.toml`, so a `codex exec` a skill starts inherits them. The block is replaced each run, other lines are kept, and keys the file already sets win. Local and Podman runs leave `config.toml` alone.
- **Authentication** is harness-specific: Claude Code uses `ANTHROPIC_API_KEY` when set, then `CLAUDE_CODE_OAUTH_TOKEN` (a subscription token from `claude setup-token`, with no OpenShell provider), and otherwise Vertex AI with gcloud ADC files; Codex uses `OPENAI_API_KEY` or local `$CODEX_HOME/auth.json` login state. The OpenShell backend requires `OPENAI_API_KEY`.
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
      Containerfile.openshell       — Claude Code sandbox image (OpenShell); extends
                                      the hardened Hummingbird harness image
    opencode/
      Containerfile                 — OpenCode runner image
      Containerfile.openshell       — OpenCode sandbox image (OpenShell); extends
                                      the hardened Hummingbird harness image
      opencode.json                 — Seed config for CI headless mode
    codex/
      Containerfile                 — Codex runner image
      Containerfile.openshell       — Codex sandbox image (OpenShell); extends
                                      the hardened Hummingbird harness image
  ci/
    Containerfile.podman            — CI environment image (podman + tools, UBI10)
    Containerfile.openshell         — CI environment image (OpenShell + podman, UBI9)
scripts/
  bump-versions.py                  — Bump pinned dependency versions in Containerfiles
```

OpenShell is consumed from UBI9 artifacts: the CLI binary copied from
`quay.io/opendatahub/odh-openshell-cli`, the gateway binary copied from
`quay.io/opendatahub/odh-openshell-gateway`, and the supervisor pulled at
runtime from `quay.io/opendatahub/odh-openshell-supervisor`. The OpenShell CI
image is UBI9, the OpenShell sandbox images are Hummingbird,
and the podman-path images stay on UBI10.

All three sandbox images build directly on their respective hardened
Hummingbird agentic images (`quay.io/aipcc/base-images/agentic/claude-code`,
`quay.io/aipcc/base-images/agentic/opencode`, and
`quay.io/aipcc/base-images/agentic/codex`, pinned by digest). The matching
digest-pinned `hi/nodejs:26-builder` image supplies `dnf` and repository
configuration through a temporary build mount. The final images retain the
Hummingbird runtime, Node.js 26, harness, `/sandbox` layout, and agentic-ci
tooling, but do not retain a package manager.

The runner-base Containerfile (`images/runner/shared/Containerfile.base`)
is pre-built as `localhost/base:latest` before building the Claude,
OpenCode, and Codex runner images. It is NOT published to any registry as
a standalone image. Do not add a CI job to push runner-base separately.

### Building locally

The sandbox images pull private base images from `quay.io/aipcc/base-images/`.
Log in before building them: `podman login quay.io`

```bash
make base-build              # build runner base image locally
make claude-build            # build Claude Code runner image (includes base)
make opencode-build          # build OpenCode runner image (includes base)
make codex-build             # build Codex runner image (includes base)
make ci-build                # build CI podman image
make openshell-claude-build  # build Claude sandbox (extends the hardened harness image)
make openshell-opencode-build # build OpenCode sandbox (extends the hardened harness image)
make openshell-codex-build   # build Codex sandbox (extends the hardened harness image)
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

## Bumping models

All model ids and effort levels live in `MODEL_REGISTRY` in `src/agentic_ci/models.py`, keyed by `--harness` name. To move a harness to a new model or effort set:

1. Edit its `HarnessModels` entry: `default` (also the classifier model), `default_effort` (every regular run, `high` everywhere), `tiers` (`high` must reuse `default`), `efforts` (values the CLI accepts), `subagent_effort` (Codex spawned agents, `None` follows the main effort), `classifier_effort`.
2. Update the defaults in `README.md` (the `--model` flag table, the env var table, and the routing tier table).
3. Run `tox -e py313`; `tests/test_models.py` checks every tier effort and the classifier effort against `efforts`.

Do not add model ids to `harness.py`; it only maps an effort value to a CLI flag. OpenCode effort values are per-model variant names (Claude 4.6 ids accept `low`/`medium`/`high`/`max`, `claude-sonnet-4-5` only `high`/`max`), so check the opencode source (`provider/transform.ts`) when changing an OpenCode model.

## Mergify

`.mergify.yml` defines merge protection rules with required CI checks. When adding, removing, or renaming jobs in `.github/workflows/`, update `.mergify.yml` to match. The file patterns in Mergify rules must stay aligned with the `paths:` filters in each workflow.

## Conventions

- Python 3.10+, uv for local dev. Minimal runtime dependencies (`requests`, `tenacity`).
- `ruff` for lint and format. Config in `pyproject.toml`. `tox` orchestrates all checks.
- Always place imports at the top of the file. No function-level or inline imports.
- Fix lint errors at the source. Don't suppress with `# noqa` or exclude files from linting.
- All tests live under `tests/`.
- `pytest` for tests.
- When functionality could be reused by multiple SDLC pipelines (e.g. autofix), expose it in `agentic-ci` as a public API rather than letting consumers call private internals across the pinned dependency boundary.
- Public API methods return curated, agentic-ci-owned data shapes — not raw wire formats. Keep field scope minimal (YAGNI); additional fields can be added later without breaking changes, provided consumers tolerate unknown fields.
- When transitive dependency incompatibilities break E2E or integration tests, constrain the dependency only in the specific test environment (e.g. `[testenv:mlflow-e2e]` in `tox.ini`) — do not change runtime dependencies. Always include a comment explaining the incompatibility and stating the conditions for removing the constraint.

## Debugging

Use the `/debug-agentic-ci` skill when investigating infrastructure-level failures. It provides a symptom catalog and structured RCA template.

When fixing a bug or adding a feature that changes failure modes, update `.claude/skills/debug-agentic-ci/references/symptoms.md` with the new pattern so future investigations have it in context.

When investigating this repo specifically, focus on these areas by symptom:

- **Container failed**: Check `backends/podman.py` or `backends/openshell/` for container launch logic. Check `harness.py` for agent CLI argument construction. Check `cli.py` for credential and OTEL setup. Check `stream.py` if output parsing failed.
- **Skills not found / wrong skills loaded**: Check `plugins.py` for install-time skill discovery (`install_opencode_skills` fallback dirs, `install_codex_plugins` native/compatibility paths, manifest generation) and runtime filtering (`enable_plugins` reads `AGENT_ENABLED_PLUGINS`). Check `harness.py` `build_env_args()` and `build_env_script_lines()` for env var forwarding to the container. Claude Code disables unwanted plugins in `settings.json`; OpenCode deletes unwanted skill directories; Codex removes unwanted native plugins and only manifest-managed compatibility skills.
- **Skill engine failure**: Check `skill.py` for the `run_skill()` flow: pre-gates, container launch, post-gates, verdict loading. Check which phase returned an error.
- **Routed run used the wrong model**: Check `routing.py` (`classify()`, `load_route()`) and `skill.py` `run_routed_skill()`. `_run/route.json` holds the classifier rating, `_run/classifier-output.txt` its raw stream, and the `skill.routed` event in `_run/claude-otel.jsonl` records the decision (`source=fallback` means the classifier failed and the default model was used). Tier defaults and accepted effort values live in `models.py` (`MODEL_REGISTRY`); the effort flag shape lives in `harness.py` (`effort_args()`).
- **MR/PR operations failed**: Check `forge.py` and the `forge` CLI subcommands. Check `git.py` for clone/push/branch operations. Check error handling in `ForgeError`.
- **Gate framework issues**: Check `gates.py` for the gate registry and execution order. Check if a gate was added or changed that altered behavior. Gates run as pre/post hooks around the agent; the wiring is in the calling repo (autofix), but the gate implementations may be here.
