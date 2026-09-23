# Local Development with OpenShell

<!-- markdownlint-disable MD046 -->

This guide takes you from zero to an AI agent working on your own
repository inside an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox on your workstation. `agentic-ci` drives the whole OpenShell
lifecycle (gateway, credential provider, sandbox, network policy), so one
command runs Claude Code, OpenCode, or Codex against your code with the
same isolation used in CI.

What you get:

- The agent runs in a sandbox with no internet access except an
  allowlist: GitHub, GitLab, PyPI, and the model API.
- Your working tree is copied into the sandbox and copied back when the
  agent finishes. The rest of your filesystem is not visible to the agent.
  Git-ignored files such as `.env` are copied too, so the agent can read
  them.
- Readable streaming output, followed by a token and cost summary.

To see what happens under the hood, read the
[OpenShell Backend](../backends/openshell.md) page.

## Prerequisites

- Linux on x86_64 with glibc 2.34 or newer (RHEL 9, Fedora 35, Ubuntu
  22.04, or later). The agentic-ci sandbox images are only published for
  x86_64.
- Rootless [Podman](https://podman.io/), with subordinate UID/GID ranges
  for your user (`grep "^$USER:" /etc/subuid` should print a line).
- `ss` and `pkill` on `PATH` (the `iproute` and `procps-ng` packages on
  Fedora and RHEL). `agentic-ci` uses them to stop the gateway.
- [uv](https://docs.astral.sh/uv/).
- An account with at least one model provider (see
  [Step 2](#step-2-set-up-credentials)).

!!! warning "Already running your own OpenShell gateway?"

    `agentic-ci` reuses whatever gateway `openshell status` reports as
    healthy. If your own gateway is active and running, agentic-ci creates
    its `ci-gcp` provider and `ci` sandbox on it. It can also delete any
    sandbox named `ci` there and kill whatever listens on port 17670, at
    the end of a run and, in newer releases, at the start of every run, so
    `--keep` does not persist. Stop your gateway before running agentic-ci.

    When no gateway is running, agentic-ci starts its own: it rewrites
    `~/.config/openshell/gateway.toml` and registers a `ci` gateway as the
    active one. Teardown removes that registration and kills whatever
    listens on port 17670, which leaves no gateway active. Run
    `openshell gateway select <name>` afterwards to switch back to yours.

## Step 1: Install the tools

Install `agentic-ci`:

```bash
uv tool install agentic-ci
```

Every agentic-ci release is tested against one OpenShell release, pinned
in
[`images/ci/Containerfile.openshell`](https://github.com/opendatahub-io/agentic-ci/blob/main/images/ci/Containerfile.openshell).
Look up the tag for the version you just installed:

```bash
AGENTIC_CI_VERSION=$(agentic-ci --version | awk '{print $2}')
OPENSHELL_TAG=$(curl -fsSL "https://raw.githubusercontent.com/opendatahub-io/agentic-ci/$AGENTIC_CI_VERSION/images/ci/Containerfile.openshell" \
    | sed -n 's/^ARG OPENSHELL_IMAGE_TAG=//p')
: "${OPENSHELL_TAG:?could not look up the OpenShell tag for agentic-ci $AGENTIC_CI_VERSION}"
echo "agentic-ci $AGENTIC_CI_VERSION uses OpenShell $OPENSHELL_TAG"
```

The lookup needs a PyPI release. A development build has no matching git
tag, so the guard above stops you there instead of carrying on with an
empty tag.

Copy the `openshell` CLI and `openshell-gateway` binaries for that
release out of the Open Data Hub images:

```bash
mkdir -p ~/.local/bin

id=$(podman create quay.io/opendatahub/odh-openshell-cli:$OPENSHELL_TAG)
podman cp "$id:/usr/local/bin/openshell" ~/.local/bin/
podman rm "$id"

id=$(podman create quay.io/opendatahub/odh-openshell-gateway:$OPENSHELL_TAG)
podman cp "$id:/usr/local/bin/openshell-gateway" ~/.local/bin/
podman rm "$id"
```

Check that these are the binaries your shell runs. An older `openshell`
earlier on `PATH` (a distro package, for example) would be used silently
and fail later with unknown flags:

```bash
for bin in openshell openshell-gateway; do
    [ "$(command -v "$bin")" = "$HOME/.local/bin/$bin" ] \
        || echo "WARNING: $bin resolves to '$(command -v "$bin")', put ~/.local/bin first on PATH"
    "$bin" --version | grep -qx "$bin ${OPENSHELL_TAG#v}" \
        || echo "WARNING: $bin is not version ${OPENSHELL_TAG#v}"
done
```

No output means both are in place.

Finally, save the versions in one file that your shell profile loads. The
gateway starts two more images for every sandbox, the supervisor and the
sandbox runtime, and both must match the binaries. Deriving everything
from `OPENSHELL_TAG` keeps them in step:

```bash
mkdir -p ~/.config/agentic-ci
cat > ~/.config/agentic-ci/openshell.env <<EOF
export AGENTIC_CI_VERSION=$AGENTIC_CI_VERSION
export OPENSHELL_TAG=$OPENSHELL_TAG
export OPENSHELL_SUPERVISOR_IMAGE=quay.io/opendatahub/odh-openshell-supervisor:\$OPENSHELL_TAG
export OPENSHELL_SANDBOX_RUNTIME_IMAGE=quay.io/opendatahub/odh-openshell-sandbox:\$OPENSHELL_TAG
EOF
. ~/.config/agentic-ci/openshell.env
```

Add `. ~/.config/agentic-ci/openshell.env` to your shell profile, once.
Without these variables the gateway falls back to the upstream images.
(Releases pinned to an OpenShell tag older than `v0.0.116-rhaiv.8` do not
use the sandbox runtime image and ignore that variable.)

!!! note "Upgrading"

    Run `uv tool upgrade agentic-ci`, then rerun this step. It rewrites
    `openshell.env`, so the binaries, the supervisor and sandbox runtime
    images, and the sandbox images in the next steps all move to the new
    release together. Newer OpenShell builds on quay.io are not supported
    until an agentic-ci release pins them.

## Step 2: Set up credentials

Pick the row that matches the harness and model provider you want:

| Harness | Provider | Setup |
|---|---|---|
| Claude Code or OpenCode | Vertex AI | `gcloud auth application-default login`, then `export ANTHROPIC_VERTEX_PROJECT_ID=<gcp-project>` |
| Claude Code or OpenCode | Anthropic API | `export ANTHROPIC_API_KEY=<key>` |
| Codex | OpenAI API | `export OPENAI_API_KEY=<key>` |

A few details worth knowing:

- `ANTHROPIC_API_KEY` wins when it is set. Unset it to use Vertex AI.
- Vertex AI defaults to the `global` region. Set `CLOUD_ML_REGION` to
  use another one.
- The credential provider reads `GOOGLE_CLOUD_PROJECT` before
  `ANTHROPIC_VERTEX_PROJECT_ID`, and `VERTEX_LOCATION` before
  `CLOUD_ML_REGION`, but Claude Code only reads the latter two. If your
  gcloud tooling exports `GOOGLE_CLOUD_PROJECT` or `VERTEX_LOCATION`,
  unset them or make them match, otherwise requests can fail with 401 or
  403.
- Codex on OpenShell requires `OPENAI_API_KEY`. A `codex login` session
  stored in `~/.codex` is not used here.
- With API-key auth, the real key can be read from inside the sandbox.
  See
  [L4 API-key exposure](../backends/openshell.md#api-key-direct-anthropic-api).

## Step 3: Run your first agent

From the root of any git repository:

```bash
cd ~/src/my-project
agentic-ci run --backend openshell \
    --image quay.io/aipcc/agentic-ci/claude-sandbox:$AGENTIC_CI_VERSION \
    --model claude-sonnet-4-5 \
    "Summarize what this repository does and how to run its tests. Do not change any files."
```

`--model claude-sonnet-4-5` keeps this smoke test cheaper than the
harness default. Drop it to use the default: the default model for each
harness is listed under `--model` in the README
[Options](https://github.com/opendatahub-io/agentic-ci#options) table,
and the full set lives in the [Model Registry](../api/models.md). The
first run takes a few minutes while Podman pulls the sandbox, supervisor,
and sandbox runtime images.

Always pass `--image` with the OpenShell backend. The
`*_CONTAINER_IMAGE` environment variables only apply to the Podman
backend.

Behind that one command, `agentic-ci`:

1. Starts a local OpenShell gateway on port 17670 and registers it as
   `ci`, unless a gateway is already running
2. Creates a credential provider from your environment
3. Creates a sandbox named `ci` and applies the network policy
4. Runs your project's setup steps on the host, if it has any
   ([Step 7](#step-7-tailor-the-sandbox-to-your-project))
5. Uploads your repository to `/sandbox/<repo-name>`
6. Runs the agent and streams its output
7. Downloads `/sandbox/<repo-name>` back over your working tree
8. Deletes the sandbox and stops the gateway

A healthy run prints `Agent exit code: 0` and a token and cost summary,
then ends with `Sandbox deleted` and `Gateway stopped`. Review the
agent's changes with `git status` and `git diff`, like you would for any
other contributor.

## Step 4: Switch harnesses

Each harness has its own sandbox image. All three are public and tagged
with the agentic-ci release they were built with. Use the tag that
matches your CLI (`$AGENTIC_CI_VERSION`): `latest` follows `main` and can
be ahead of the PyPI release.

| Harness | `--harness` | `--image` |
|---|---|---|
| Claude Code | `claude-code` (default) | `quay.io/aipcc/agentic-ci/claude-sandbox:$AGENTIC_CI_VERSION` |
| OpenCode | `opencode` | `quay.io/aipcc/agentic-ci/opencode-sandbox:$AGENTIC_CI_VERSION` |
| Codex | `codex` | `quay.io/aipcc/agentic-ci/codex-sandbox:$AGENTIC_CI_VERSION` |

```bash
# OpenCode on Vertex AI
agentic-ci run --backend openshell --harness opencode \
    --image quay.io/aipcc/agentic-ci/opencode-sandbox:$AGENTIC_CI_VERSION \
    "Add type hints to utils.py"

# OpenCode with an Anthropic API key: --model is required, because the
# default OpenCode model is a Vertex AI id
agentic-ci run --backend openshell --harness opencode \
    --image quay.io/aipcc/agentic-ci/opencode-sandbox:$AGENTIC_CI_VERSION \
    --model anthropic/claude-sonnet-4-5-20250929 \
    "Add type hints to utils.py"

# Codex
agentic-ci run --backend openshell --harness codex \
    --image quay.io/aipcc/agentic-ci/codex-sandbox:$AGENTIC_CI_VERSION \
    "Add type hints to utils.py"
```

The other `run` flags, such as `--effort`, `--no-otel`, and extra agent
arguments after `--`, work as they do on the other backends. See
[Options](https://github.com/opendatahub-io/agentic-ci#options) in the
README.

## Step 5: Keep the gateway and sandbox between runs

By default each run starts the gateway, creates a fresh sandbox, and
tears both down at the end. For a series of runs, add `--keep` to leave
them up. The next run skips gateway startup and sandbox creation, and
picks up the files exactly as the agent left them:

```bash
agentic-ci run --backend openshell --keep \
    --image quay.io/aipcc/agentic-ci/claude-sandbox:$AGENTIC_CI_VERSION \
    "Write a failing test for the bug described in BUG.md"

agentic-ci run --backend openshell --keep \
    --image quay.io/aipcc/agentic-ci/claude-sandbox:$AGENTIC_CI_VERSION \
    "Now make that test pass"
```

To start over with a fresh sandbox but keep the gateway running, delete
only the sandbox. The next `run --keep` creates a new one on the running
gateway, reuses the credential provider, and uploads your working tree
again:

```bash
openshell sandbox delete ci
```

When you are done, tear everything down:

```bash
agentic-ci stop --backend openshell
```

A run without `--keep` also tears everything down when it finishes.
`stop` needs `--backend openshell` too, otherwise it targets the default
Podman backend. Changing `--harness`, `--image`, or the auth mode (for
example exporting `ANTHROPIC_API_KEY`) recreates the sandbox
automatically.

Run every command from a shell that has sourced
`~/.config/agentic-ci/openshell.env`. If the supervisor or sandbox runtime
image differs from the one the gateway was started with, as in an IDE
terminal or a shell opened before you edited your profile, newer
releases restart the gateway. That deletes the kept sandbox and only logs
`OpenShell gateway config changed; restarting gateway`. Your working tree
is safe, since each run downloads it back, but anything installed inside
the sandbox is gone.

!!! warning "A kept sandbox does not see your local edits"

    Your repository is uploaded only when the sandbox is created. Later
    runs keep working on the sandbox's copy, and the download at the end
    of each run writes that copy back over your working tree, `.git`
    included. Local edits, commits, or branch switches made between runs
    are not seen by the agent and can be rolled back. Run
    `openshell sandbox delete ci` before you touch the repository locally,
    so the next run starts from a fresh upload.

Delete the sandbox the same way in two more cases:

- **Switching repositories.** A kept sandbox is tied to the repository it
  was created from, and running from another directory does not replace
  it.
- **Getting the cost summary back.** Each run's telemetry collector
  listens on a new port, and the network policy only allows the port of
  the run that created the sandbox, so the token and cost summary can
  come out empty on runs that reuse it.

## Step 6: Look inside the sandbox

While a sandbox is up, use the `openshell` CLI directly. The sandbox is
always named `ci`:

```bash
openshell sandbox list                         # what is running
openshell sandbox connect ci                   # interactive shell, repo in /sandbox/<repo-name>
openshell sandbox connect ci --editor vscode   # open the sandbox in VS Code over SSH
openshell sandbox exec --name ci -- git -C /sandbox/my-project log --oneline -5
openshell logs ci --tail                       # stream sandbox and gateway logs
openshell policy get ci --full                 # the network policy in effect
openshell term                                 # the OpenShell TUI
```

`openshell logs` is the first place to look when the agent reports a
network error.

## Step 7: Tailor the sandbox to your project

The default policy only allows GitHub, GitLab, PyPI, and the model API
(see [Network Policy](../backends/openshell.md#network-policy) for the
full list). Two files in your repository change that, and CI reads the
same files.

`.agentic-ci/openshell-policy.yml` adds endpoints the agent may reach:

```yaml
endpoints:
  - "registry.npmjs.org:443:read-only"
```

`.agentic-ci/config.yml` lists setup steps. They run directly on your
machine, outside the sandbox and with full network access, before the
repository is uploaded. Use them to install dependencies the agent needs
but cannot download itself, and only run repositories you trust:

```yaml
setup:
  - name: Install dependencies
    run: npm ci
```

Both files are read when the sandbox is created, so run
`openshell sandbox delete ci` after editing them. To try out a policy
without committing it, pass `--policy my-policy.yml`. Its endpoints
replace the repository file's, and the defaults still apply. A relative
path is resolved from your current directory, not from `--workdir`. If
the path does not exist, agentic-ci silently uses the repository file (or
only the defaults when there is none), so check that the
`Policy source:` line in the output names your file. See
[Project Configuration](../configuration.md) for the full reference.

## Alternative: run everything in a container

If you would rather not install anything on the host, the
`quay.io/aipcc/agentic-ci/openshell` image ships `agentic-ci`, the
OpenShell CLI and gateway, and matching `OPENSHELL_SUPERVISOR_IMAGE` and
`OPENSHELL_SANDBOX_RUNTIME_IMAGE` values. It needs `--privileged` because
OpenShell starts sandboxes with a nested Podman:

```bash
podman run -d --name openshell-dev --privileged \
    -v "$PWD:/workspace:z" \
    -v ~/.config/gcloud/application_default_credentials.json:/root/.config/gcloud/application_default_credentials.json:ro,z \
    -e ANTHROPIC_VERTEX_PROJECT_ID \
    -e CLOUD_ML_REGION \
    quay.io/aipcc/agentic-ci/openshell sleep infinity

podman exec -it openshell-dev \
    agentic-ci run --backend openshell --workdir /workspace \
    --image quay.io/aipcc/agentic-ci/claude-sandbox \
    "Summarize what this repository does"

podman rm -f openshell-dev
```

Both images use the `latest` tag here, which CI builds from the same
`main` commit, so they match each other. To pin a release instead, use
the same version tag on both. For API-key auth, pass
`-e ANTHROPIC_API_KEY` or `-e OPENAI_API_KEY` instead of the gcloud mount
and Vertex variables. Inside the sandbox the repository lives in
`/sandbox/workspace`, and the agent's changes land back in `/workspace`,
which is your repository on the host. Sandbox images are pulled inside
the container, so they are downloaded again each time you recreate it.
This is the same setup the OpenShell end-to-end tests use.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `FileNotFoundError` naming `openshell` or `openshell-gateway` | The binaries are not on `PATH`. `~/.local/bin` must be on it, ahead of any other OpenShell install (see the check in [Step 1](#step-1-install-the-tools)). |
| `Gateway did not become healthy within 60s` | Read the newest `~/.local/state/openshell/gateway-*.log`. `failed to parse gateway config file` means the gateway binary and agentic-ci come from different releases: rerun [Step 1](#step-1-install-the-tools). |
| The `ci-gcp` provider or `ci` sandbox shows up on your own gateway | agentic-ci reused a gateway that was already running. Stop it before running agentic-ci (see [Prerequisites](#prerequisites)). |
| `run agentic-ci stop before switching harnesses` | A previous run left state behind. Run `agentic-ci stop --backend openshell` and retry. |
| `OpenShell Codex runs require OPENAI_API_KEY` | Export `OPENAI_API_KEY`. Codex login state is not used with OpenShell. |
| The agent cannot reach a host | Add the endpoint to `.agentic-ci/openshell-policy.yml` ([Step 7](#step-7-tailor-the-sandbox-to-your-project)) and recreate the sandbox. |
| The sandbox disappears mid-run and the next command reports `sandbox is not ready` | Most likely an out-of-memory kill: `journalctl -k` shows `Memory cgroup out of memory`. The CLI does not set resource limits yet. See [Sandbox Resources](../backends/openshell.md#sandbox-resources) for the Python API. |
| Vertex AI requests fail with 401 or 403 | Check that `GOOGLE_CLOUD_PROJECT` and `VERTEX_LOCATION` are unset or match your Vertex variables ([Step 2](#step-2-set-up-credentials)). If your login expired, run `gcloud auth application-default login`, then `agentic-ci stop --backend openshell` so the provider is recreated. |
