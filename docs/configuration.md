# Project Configuration

Repositories can configure agentic-ci behavior by placing files in a
`.agentic-ci/` directory at the repository root.

| File | Purpose |
|------|---------|
| `config.yml` | General project configuration (setup steps, etc.) |
| `openshell-policy.yml` | Additional network endpoints for the OpenShell sandbox |

## Setup Steps

Setup steps are commands that run on the host **before** the workdir is
uploaded into the sandbox. They execute with full network access, making
them ideal for dependency installation that the sandboxed agent cannot
perform itself.

This is primarily useful for the **OpenShell backend**, where the sandbox
has no internet access by default. The Podman and Local backends already
have network access, so setup steps are not needed there.

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

### How It Works

1. The sandbox is created and the network policy is applied
2. Setup steps run sequentially on the host in the workdir
3. The workdir (now including setup step outputs like `node_modules/`) is
   uploaded into the sandbox
4. The agent starts inside the sandbox

```
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

### Behavior

- Steps run with `shell=True`, so pipes, redirects, and shell builtins
  work as expected.
- Steps run sequentially in the order they appear in the config.
- If a step fails (non-zero exit code), the run aborts with an error.
- Each step has a 10-minute timeout to prevent hanging commands from
  blocking CI indefinitely.
- Malformed entries (e.g. missing `run` key, non-string `run` value) are
  skipped with a warning.

### Example: Node.js Project

```yaml
# .agentic-ci/config.yml
setup:
  - name: Install dependencies
    run: npm ci
```

This ensures `node_modules/` is present inside the sandbox so the agent
can run tests, linting, and type checks without needing internet access.
