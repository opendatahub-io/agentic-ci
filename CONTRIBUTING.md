# Contributing to agentic-ci

## Getting Started

```bash
git clone git@github.com:opendatahub-io/agentic-ci.git
cd agentic-ci
uv sync --all-extras
```

## Development Workflow

1. Create a feature branch from `main`. Never commit directly to `main`.
2. Make your changes following the conventions below.
3. Run all verification checks (see below).
4. Open a PR with a clear description of what changed and why.

## PR Requirements

Every PR must satisfy these before merge:

### Verification checks

Run all four checks and fix any failures:

```bash
tox -e py313          # unit tests
tox -e lint           # ruff lint
tox -e check-format   # ruff format check
tox -e typecheck      # mypy type check
```

### Unit tests

New features and bug fixes must include unit tests. Tests live under
`tests/` and use `pytest`.

### E2E tests

Changes to backends, harnesses, or the CLI must include or update the
relevant end-to-end tests under `tests/e2e/`. E2E tests exercise the
full execution path with real containers and API calls:

- `e2e-claude-runner.sh` -- Claude Code runner (podman backend)
- `e2e-opencode-runner.sh` -- OpenCode runner (podman backend)
- `e2e-cursor-runner.sh` -- Cursor runner (podman backend)
- `e2e-local-runner.sh` -- Local backend (no container)
- `e2e-openshell-sandbox.sh` -- OpenShell sandbox backend
- `test_mlflow_e2e.py` -- MLflow integration

If your change affects a specific backend or harness, update the
corresponding e2e test. If you add a new backend or harness, add a
new e2e test script.

### Documentation

Update the relevant docs when your PR changes user-facing behavior:

- `README.md` for CLI flags, environment variables, usage examples
- `CLAUDE.md` / `AGENTS.md` for architecture or convention changes
- `docs/` for API docs or guides
- Docstrings for public API changes (used by `tox -e docs`)

## Harness and Backend Parity

agentic-ci supports multiple harnesses (Claude Code, OpenCode, Cursor) and
backends (local, podman, OpenShell). When making changes to one, you
must update the others to keep feature parity:

### Harnesses

If you add a feature or fix to one harness, apply the equivalent
change to all harnesses. For example:

- A new CLI argument added to `ClaudeCodeHarness.build_args()` needs
  the equivalent in `OpenCodeHarness.build_args()` and `CursorHarness.build_args()`.
- A new env var handled in one harness's `build_env_args()` must be
  handled in all.
- Stream processor changes in `ClaudeCodeStreamProcessor` should have
  the corresponding update in `OpenCodeStreamProcessor` and
  `CursorStreamProcessor` when the feature applies to all (e.g., token
  display, tool call summaries).

### Backends

If you add a capability to one backend, the abstract `Backend` class
and all implementations must stay consistent:

- A new `setup()` or `run()` parameter added to one backend should be
  supported by the others, or explicitly documented as backend-specific
  with a clear reason.
- Container image changes (Containerfiles) must be applied across all
  harness variants (Claude, OpenCode, Cursor), plus their OpenShell
  sandbox variants.

### Container images

Runner images come in sets (Claude + OpenCode + Cursor) and optionally
in OpenShell sandbox variants. When changing `images/runner/`:

- Changes to `shared/Containerfile.base` affect all runners.
- Changes specific to one runner (e.g., `claude-code/Containerfile`)
  likely need equivalent updates in `opencode/Containerfile` and
  `cursor/Containerfile`.
- OpenShell sandbox images (`*.openshell`) must stay aligned with their
  non-sandbox counterparts.

If a feature genuinely applies to only one harness or backend, document
the reason in the PR description.

## Code Conventions

- Python 3.10+. Minimal runtime dependencies.
- `ruff` for lint and format. Config in `pyproject.toml`.
- Imports at the top of the file. No function-level or inline imports.
- Fix lint errors at the source. No `# noqa` suppressions.
- All tests under `tests/`.

## Mergify

`.mergify.yml` defines required CI checks. When adding, removing, or
renaming jobs in `.github/workflows/`, update `.mergify.yml` to match.
File patterns in Mergify rules must stay aligned with `paths:` filters
in each workflow.
