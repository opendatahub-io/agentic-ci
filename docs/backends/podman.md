# Podman Backend

The Podman backend runs AI agents inside a standard Podman container. It
is the default backend and works anywhere Podman is available.


## How It Works

The backend creates a long-running detached container, then execs the
agent inside it. The work directory and GCP credentials are bind-mounted
into the container. On completion, the container is removed.

1. Resolve the container image (from `--image`, env var, or error)
2. Stage GCP credentials to a temp directory (Vertex AI auth only)
3. Start a detached container with `sleep <timeout>`
4. Exec the agent CLI inside the container
5. Stop the container and restore the host's git control files
6. Remove the container on completion

The bind mount gives the agent write access to the host repository's
`.git`. The backend records the host's git control files (`.git/config`,
`config.worktree`, `commondir`, `hooks/`, `info/`) before the container
starts. When the agent exits, `run()` stops the container with
`podman stop --time 0`, which kills every process the agent left running
(the kernel tears down the container's whole PID namespace), and only then
restores the host copy. Host-side git after the run, including
`--post-gates`, therefore never honors hooks, fsmonitor, filter or diff
drivers or other config the agent set, while the agent's commits are kept.
The next `run()` in the same process records the host's git control files
again, so host-side config changes made between runs are kept, and then
starts the stopped container, so its filesystem carries over between runs,
but no process does. If the
container cannot be stopped it is removed. If it can be neither stopped nor
removed, an agent process may still be running, so instead of restoring,
the backend moves `.git` aside and deletes it (the agent's commits are lost)
and the podman error propagates. `stop()` restores the host copy once more
after removing the container, with the same fallback if removal fails. See
[Host git after the run](openshell.md#host-git-after-the-run) for details.

With `run --keep`, the container is left in place but stopped. The
container name is unique to each `agentic-ci` process, so a separate
`agentic-ci stop` process neither finds that container nor restores
anything; remove it with `podman rm`.

## Podman Commands

### Setup

```bash
# Remove any leftover container from a previous run
podman rm -f agentic-ci

# Pull the image (skipped for localhost/ images)
podman pull <IMAGE>

# Start a detached container
podman run -d \
  --name agentic-ci \
  --pull never \
  --network host \
  --userns=keep-id:uid=1000,gid=1000 \    # rootless
  --env CLAUDE_CODE_USE_VERTEX=1 \          # harness env vars
  --env CLOUD_ML_REGION=global \
  --env ANTHROPIC_VERTEX_PROJECT_ID=<PROJECT> \
  --env DISABLE_AUTOUPDATER=1 \
  -v <WORKDIR>:/workspace:z \               # work directory
  -v <ADC>:<HOME>/.config/gcloud/application_default_credentials.json:ro,z \
  -v <CONFIG>:<HOME>/.config/gcloud/configurations/config_default:ro,z \
  --workdir /workspace \
  <IMAGE> \
  sleep 1200
```

When running as root (CI), `--user 1000:1000` is used instead of
`--userns`, and the workdir is chowned to 1000:1000 before container
creation.

### Run

```bash
podman exec \
  --env AGENT_MODEL=<MODEL> \
  --env AGENT_REASONING_EFFORT=<EFFORT> \   # unless effort is none
  --env CLAUDE_CODE_ENABLE_TELEMETRY=1 \    # OTEL vars (if enabled)
  --env OTEL_METRICS_EXPORTER=otlp \
  --env OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:<PORT> \
  agentic-ci \
  claude --permission-mode bypassPermissions --model <MODEL> \
  --output-format stream-json --include-partial-messages --verbose \
  -p "<PROMPT>"
```

### Teardown

```bash
podman rm -f agentic-ci
```

## Authentication

The backend auto-detects the auth mode from the environment:

- **API key**: If `ANTHROPIC_API_KEY` is set, it is passed directly via
  `--env ANTHROPIC_API_KEY`. No credential files are mounted.

- **OAuth token** (Claude Code only): If `ANTHROPIC_API_KEY` is not set
  and `CLAUDE_CODE_OAUTH_TOKEN` is, the token is passed via
  `--env CLAUDE_CODE_OAUTH_TOKEN`. No credential files are mounted.

- **Vertex AI**: GCP credentials are staged to a temp directory and
  bind-mounted read-only into the container.

### Credential Resolution (Vertex AI)

The backend searches for GCP credentials in this order:

1. `GCLOUD_CREDENTIALS` env var (raw JSON or base64-encoded)
2. `GCP_SERVICE_ACCOUNT_KEY` env var or file path (raw JSON or base64-encoded)
3. `~/.config/gcloud/application_default_credentials.json` (default ADC path)
4. `GOOGLE_APPLICATION_CREDENTIALS` env var (file path)

The resolved credentials are written to a temp directory along with
a gcloud config file (`config_default` with the project ID). Both are
bind-mounted into the container at the harness's credential mount target
(default: `/home/agent-ci/.config/gcloud/`).

## Container Images

The image is resolved from (in priority order):

1. `--image` CLI flag
2. Harness-specific env var (`CLAUDE_CONTAINER_IMAGE` or `OPENCODE_CONTAINER_IMAGE`)
3. Error if neither is set

Images prefixed with `localhost/` are treated as local builds and skip
the `podman pull` step.

Standard images:

| Harness | Image |
|---------|-------|
| Claude Code | `quay.io/aipcc/agentic-ci/claude-runner:latest` |
| OpenCode | `quay.io/aipcc/agentic-ci/opencode-runner:latest` |

## Environment Variables

### Passed to the container (`podman run --env`)

Vertex AI auth:

| Variable | Value |
|----------|-------|
| `CLAUDE_CODE_USE_VERTEX` | `1` |
| `CLOUD_ML_REGION` | From env (default: `global`) |
| `ANTHROPIC_VERTEX_PROJECT_ID` | From env |
| `DISABLE_AUTOUPDATER` | `1` |

API key auth:

| Variable | Value |
|----------|-------|
| `ANTHROPIC_API_KEY` | From env (passed by reference, not value) |
| `DISABLE_AUTOUPDATER` | `1` |

OAuth token auth (Claude Code):

| Variable | Value |
|----------|-------|
| `CLAUDE_CODE_OAUTH_TOKEN` | From env (passed by reference, not value) |
| `DISABLE_AUTOUPDATER` | `1` |

### Passed at exec time (`podman exec --env`)

| Variable | Value |
|----------|-------|
| `AGENT_MODEL` | The model being used |
| `AGENT_REASONING_EFFORT` | The effective reasoning effort (omitted when effort is `none`) |
| OTEL vars | Only when `--no-otel` is not set |

### Extra env vars

Consumers (like the code-review workflow) can pass additional env vars
via the `extra_env` parameter. These are added as `--env KEY=VALUE`
flags on `podman run`.

## Networking

The container uses `--network host`, which means it shares the host's
network stack. This is required for:

- OTEL collector access (runs on the host, not in the container)
- Direct API access to Anthropic or Vertex AI endpoints

## Volume Mounts

| Host Path | Container Path | Mode |
|-----------|---------------|------|
| Work directory | `/workspace` | read-write |
| ADC credentials | `<HOME>/.config/gcloud/application_default_credentials.json` | read-only |
| gcloud config | `<HOME>/.config/gcloud/configurations/config_default` | read-only |

Credential mounts are only present for Vertex AI auth. The `:z` suffix
enables SELinux relabeling for rootless podman.

## Differences from OpenShell Backend

| Aspect | Podman | OpenShell |
|--------|--------|-----------|
| Isolation | Standard container | Sandbox with Landlock, network policy |
| Network | Host networking | Policy-controlled egress |
| Credentials | Bind-mounted files | Provider + metadata emulator |
| Auth inside container | Agent authenticates directly | Supervisor proxy handles auth |
| OTEL | Host-accessible (network host) | Requires gateway IP routing |
| Timeout | `sleep <timeout>` (default 1200s) | Sandbox lifecycle managed by gateway |
