# Project Configuration

Repositories can configure agentic-ci behavior by placing files in a
`.agentic-ci/` directory at the repository root.

| File | Purpose |
|------|---------|
| `config.yml` | `sandbox:` is the repo overlay of a [sandbox profile](sandbox-profiles.md): toolchains, egress presets, setup and validate steps that run in the sandbox. `setup:` is the legacy host setup, deprecated and off by default. |
| `openshell-policy.yml` | Additional network endpoints for the OpenShell sandbox, used only when no sandbox profile is set |

## Sandbox Overlay

The `sandbox:` section of `.agentic-ci/config.yml` is how a repository
tells agentic-ci what it needs inside the sandbox: toolchains from the
[catalog](sandbox-profiles.md#catalog), read-only egress presets, setup
steps that install dependencies before the agent, validate commands that
check the agent's change, declared skips and non-secret environment
variables.

```yaml
# .agentic-ci/config.yml
sandbox:
  toolchains:
    node: "22"
    pnpm: auto
  egress:
    - npm
  setup:
    - name: install
      run: pnpm install --frozen-lockfile
      timeout: 1800
  validate:
    - {name: lint, kind: lint, run: pnpm run lint}
    - {name: unit, kind: test, run: pnpm run test:unit, timeout: 1800}
```

agentic-ci does not read this section by itself. The caller reads it from a
commit it trusts, such as the base branch rather than the agent's working
tree, parses it with `parse_profile(section, source="overlay")`, merges it
with its own central profile with `merge_profiles()` and passes the result as
`SkillConfig.sandbox_profile`. An overlay cannot widen what the caller
allows: raw egress, `resources` and `overlay` are dropped, and only the
`pypi`, `npm` and `goproxy` presets are accepted by default. See
[Central Profiles and Repo Overlays](sandbox-profiles.md#central-profiles-and-repo-overlays)
and [Merge Precedence](sandbox-profiles.md#merge-precedence) for the rules,
and [Sandbox Profiles](sandbox-profiles.md) for every field.

[Write a Sandbox Overlay](guides/sandbox-overlay.md) walks a repo owner
through writing one. The autofix bot reads the overlay from the target
repo's base branch; its
[overlay page](https://gitlab.com/redhat/rhel-ai/agentic-ci/autofix/-/blob/main/docs/operations/sandbox-overlay.md)
covers what autofix adds.

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

## Setup Steps (deprecated host path)

!!! warning "Deprecated, off by default, refused in CI"
    Use the `setup` steps of a [sandbox overlay](#sandbox-overlay) instead.
    They run inside the sandbox with only the profile's egress presets open
    and no credential. Host setup steps run on your machine, outside any
    sandbox.

The top-level `setup:` list of `.agentic-ci/config.yml` holds commands that
run on the host before the agent starts (for the OpenShell backend, before
the workdir is uploaded). They run only when the caller opts in with
`--allow-host-setup` (CLI `setup` and `run`) or
`SkillConfig.allow_host_setup=True`:

```yaml
# .agentic-ci/config.yml
setup:
  - name: Install dependencies   # optional label
    run: npm ci
  - npm ci                       # bare-string shorthand
```

- Without the opt-in, nothing runs and agentic-ci logs one line with the
  number of steps it skipped. `AGENTIC_CI_SKIP_SETUP=1` skips them even
  with the opt-in, without a log line; autofix sets it.
- The opt-in is for a developer running their own repository on their own
  machine. It logs a deprecation warning, and the CLI refuses it with a
  usage error when `CI`, `GITLAB_CI` or `GITHUB_ACTIONS` is set to anything
  other than empty, `0` or `false`.
- Steps run in order in the workdir with `shell=True` and a 10-minute
  timeout each. A failed step aborts the run; a malformed entry is skipped
  with a warning.
- Each step gets an environment built from scratch: `PATH=/usr/local/bin:/usr/bin:/bin`,
  a temporary `HOME` shared by the steps of one run, and
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null` and
  `GIT_CONFIG_NOSYSTEM=1`. Tokens in your environment are not passed on.
- Each step runs in its own process group, which is killed when the step
  ends. The workdir's `.git` control files (`config`, `config.worktree`,
  `commondir`, `hooks/`, `info/`) are restored after the last step, also
  when a step fails.
- The Podman and local backends run the steps on the host during `setup`
  too.

!!! danger "Not a sandbox"
    The clean environment keeps secrets away from tools that read them from
    the environment, but a hostile step still runs as you and can read any
    file you can read (for example `~/.config` or `~/.ssh`). The
    process-group kill and the `.git` restore are best-effort cleanup, not
    containment: a process that leaves the step's process group (`setsid`,
    `setpgid`, or shell job control such as `set -m`) keeps running, and
    submodule git dirs (`.git/modules`), linked worktrees' git dirs and
    anything outside the workdir's own `.git` are not restored. Only run
    your own repository's steps this way.
