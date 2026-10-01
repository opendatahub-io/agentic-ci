# Project Configuration

Repositories can configure agentic-ci behavior by placing files in a
`.agentic-ci/` directory at the repository root.

| File | Purpose |
|------|---------|
| `config.yml` | General project configuration (host setup steps, deprecated and off by default) |
| `openshell-policy.yml` | Additional network endpoints for the OpenShell sandbox |

## Setup Steps (deprecated host path)

!!! warning "Off by default, deprecated, refused in CI"
    The `setup:` steps of `.agentic-ci/config.yml` no longer run unless the
    caller opts in with `--allow-host-setup` (CLI `setup` and `run`) or
    `SkillConfig.allow_host_setup=True`. Without the opt-in, nothing runs
    and agentic-ci logs one line with the number of steps it skipped. The
    opt-in is meant for a developer running their own repository on their
    own machine: it is refused (the CLI exits with a usage error) when `CI`,
    `GITLAB_CI` or `GITHUB_ACTIONS` is set to anything other than empty,
    `0` or `false`, and it logs a deprecation warning. For dependency
    installation in CI, use a [sandbox profile](sandbox-profiles.md)'s
    `setup` steps, which run inside the OpenShell sandbox.

Setup steps are commands that run on the host **before** the agent starts
(for the OpenShell backend, before the workdir is uploaded into the
sandbox). They execute on the host with its network access, outside any
sandbox.

### Configuration

Add a `setup` key to `.agentic-ci/config.yml`:

```yaml
# .agentic-ci/config.yml
setup:
  - name: Install dependencies
    run: npm ci
  - name: Build project
    run: npm run build
```

Both object form and bare-string shorthand are supported:

```yaml
# Object form (recommended for clarity)
setup:
  - name: Install dependencies
    run: npm ci

# Bare-string shorthand
setup:
  - npm ci
```

Each step object accepts:

| Field | Required | Description |
|-------|----------|-------------|
| `run` | yes | Shell command to execute |
| `name` | no | Human-readable label for log output |

Run them locally with the opt-in, outside CI:

```bash
agentic-ci run --backend openshell --allow-host-setup "..."
```

### How It Works

With `--allow-host-setup` on the OpenShell backend:

1. The sandbox is created and the network policy is applied
2. Setup steps run sequentially on the host in the workdir
3. The workdir (now including setup step outputs like `node_modules/`) is
   uploaded into the sandbox
4. The agent starts inside the sandbox

```text
Host (internet access)          Sandbox (isolated)
─────────────────────           ──────────────────
1. sandbox.create()
2. npm ci  ─────────────┐
   (setup step)         │
3. sandbox.upload() ────┼──→  /sandbox/repo/
                        │     ├── node_modules/  ✓
                        │     ├── src/
                        │     └── ...
4.                      └──→  agent starts
```

The Podman and local backends run the steps on the host during `setup` too,
before the container starts or the agent runs.

### Behavior

- Steps run with `shell=True`, so pipes, redirects, and shell builtins
  work as expected. The repository controls the command either way.
- Steps run sequentially in the workdir, in the order they appear in the
  config.
- Each step gets an environment built from scratch, not inherited:
  `PATH=/usr/local/bin:/usr/bin:/bin`, a temporary `HOME` shared by the
  steps of one run and removed afterwards, and
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null` and
  `GIT_CONFIG_NOSYSTEM=1`. Tokens and keys in your environment (such as
  `OPENAI_API_KEY` or `GITHUB_TOKEN`) are therefore not passed to tools that
  read them from the environment, and your global git config is not used.
- If a step fails (non-zero exit code), the run aborts with an error.
- Each step has a 10-minute timeout.
- Each step runs in its own session and process group, which is killed
  when the step's shell exits or times out, so background processes it
  started do not keep running.
- The workdir's `.git` control files (`config`, `config.worktree`,
  `commondir`, `hooks/`, `info/`) are recorded before the first step and
  restored after the last one, also when a step fails; the names of the
  restored entries are logged.
- Malformed entries (e.g. missing `run` key, non-string `run` value) are
  skipped with a warning.
- Set `AGENTIC_CI_SKIP_SETUP=1` to skip all setup steps, even with the
  opt-in; nothing is logged then. autofix sets it, so autofix runs log
  nothing about host setup.

!!! danger "Not a sandbox"
    The clean environment keeps secrets away from tools that read them from
    the environment, but a hostile step still runs as you and can read any
    file you can read (for example `~/.config` or `~/.ssh`). The
    process-group kill and the `.git` restore are best-effort cleanup, not
    containment: a process that leaves the step's process group (`setsid`,
    `setpgid`, or shell job control such as `set -m`) keeps running, and
    submodule git dirs (`.git/modules`), linked worktrees' git
    dirs and anything outside the workdir's own `.git` are not restored.
    Only run your own repository's steps this way; use a sandbox profile
    for containment.

### Example: Node.js Project

```yaml
# .agentic-ci/config.yml
setup:
  - name: Install dependencies
    run: npm ci
```

With `--allow-host-setup`, this makes `node_modules/` present inside the
sandbox so the agent can run tests, linting, and type checks without
needing general internet access. In CI, declare the same install as a
sandbox profile `setup` step with the `npm` egress preset instead.

## Network Policy (OpenShell)

Projects can declare additional network endpoints for the OpenShell
sandbox in `.agentic-ci/openshell-policy.yml`. These are merged with
the built-in defaults (duplicates are ignored).

```yaml
# .agentic-ci/openshell-policy.yml
endpoints:
  - "redhat.atlassian.net:443:read-only"
  - "*.example.com:443:full"
```

Each endpoint uses the format `host:port:access` where access is one of
`full`, `read-only`, or `read-write`.

The `--policy` CLI flag takes precedence: if a flag path is provided and
the file exists, the repo-level file is ignored.

When the caller passes a [sandbox profile](sandbox-profiles.md#egress)
(for example autofix, through `SkillConfig.sandbox_profile`), the repo-level
file is ignored as well and egress comes from the profile.

See [OpenShell Backend](backends/openshell.md) for the full list of
built-in default endpoints.
