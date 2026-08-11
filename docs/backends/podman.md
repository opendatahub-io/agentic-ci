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
5. Remove the container on completion

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
2. Harness-specific env var (`CLAUDE_CONTAINER_IMAGE`, `OPENCODE_CONTAINER_IMAGE`, or `CURSOR_CONTAINER_IMAGE`)
3. Error if neither is set

Images prefixed with `localhost/` are treated as local builds and skip
the `podman pull` step.

Standard images:

| Harness | Image |
|---------|-------|
| Claude Code | `quay.io/aipcc/agentic-ci/claude-runner:latest` |
| OpenCode | `quay.io/aipcc/agentic-ci/opencode-runner:latest` |
| Cursor | `quay.io/aipcc/agentic-ci/cursor-runner:latest` |

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

### Passed at exec time (`podman exec --env`)

| Variable | Value |
|----------|-------|
| `AGENT_MODEL` | The model being used |
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
