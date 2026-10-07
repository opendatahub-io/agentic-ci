# Write a Sandbox Overlay

This guide is for owners of a repository that an agentic-ci pipeline (for
example the autofix bot) runs agents against. It shows how to tell agentic-ci
what your repo needs, so the agent can install your dependencies and run
your checks before it proposes a change.

## Why add an overlay

The agent runs in an OpenShell sandbox. Without a profile, the sandbox has a
fixed set of tools and cannot reach most package registries. The agent then
often cannot build your code or run your tests, so it can only guess whether
its change works.

An overlay is a `sandbox:` section in `.agentic-ci/config.yml` at the root of
your repo. With it:

- The sandbox gets the language toolchains your repo uses, at the versions
  you choose.
- Setup steps install your dependencies before the agent starts.
- The agent runs your lint, build and test commands while it works.
- After the agent stops, agentic-ci runs the same commands again and records
  the results, which the pipeline can show you.

## What the sandbox has

- **Tools:** bash, git, make, curl, jq, `python3` (3.14) and `python3.12`,
  `uv`, Node.js with npm, `gh`, `glab`, `ruff`, `shellcheck` and `shfmt`. No
  system package manager, browsers or container runtime.
- **Network for setup and validate steps:** only the
  [egress presets](#3-open-egress-presets) the profile opens.
- **Network for the agent:** the presets the profile opens, plus the
  backend's built-in endpoints (GitHub, GitLab, PyPI and the model API; see
  [Network Policy](../backends/openshell.md#network-policy)).
- **Credentials:** setup and validate steps get none: no forge, Jira or model
  credential. The agent can use its model authentication and whatever the
  pipeline gives it.

Anything else must come from a [toolchain](#2-choose-toolchains) or from a
setup step that downloads it through a preset. See
[Phases and the setup shim](../sandbox-profiles.md#phases-and-the-setup-shim)
for how the phases are kept apart.

## Step by step

### 1. Create the file

Add a `sandbox:` section to `.agentic-ci/config.yml` in your repo. If the
file has other sections, keep them. Every field is optional.

```yaml
sandbox:
  toolchains: {}
  egress: []
  env: {}
  setup: []
  validate: []
  skips: []
```

### 2. Choose toolchains

Toolchains come from the [catalog](../sandbox-profiles.md#catalog). The
host downloads each one and checks it against the official checksum before
it goes into the sandbox. A toolchain needs no egress preset.

```yaml
  toolchains:
    go: auto          # from go.mod at the repo root
    node: "22"        # newest 22.x
    pnpm: auto        # from packageManager in package.json
    golangci-lint: "2.13.2"
```

- `auto` reads a version from repo files for some tools; see
  [`auto` sources](../sandbox-profiles.md#auto-sources).
- Some tools also accept a partial version such as `"22"` or `"1.26"`; see
  [Version forms](../sandbox-profiles.md#version-forms). The others need an
  exact version.
- Quote versions that contain a dot. YAML reads `1.20` as the number `1.2`.

### 3. Open egress presets

An overlay can open the `pypi`, `npm` and `goproxy` presets. Each allows only
read requests (`GET`, `HEAD`, `OPTIONS`) to its hosts; see
[Egress](../sandbox-profiles.md#egress) for the hosts.

```yaml
  egress:
    - goproxy
```

### 4. Add setup steps

Setup steps run once, in order, inside the sandbox and before the agent
starts. Use them to install dependencies and tools.

```yaml
  setup:
    - name: modules
      run: go mod download
      timeout: 900      # seconds, 1 to 3600, default 600
```

- A name has only letters, digits, `.`, `_` and `-`.
- A step that fails does not stop the run. The agent is told which step
  failed and sees the last lines of its output.
- A setup step cannot start a service for later steps or for the agent.
  Every process it leaves is stopped when the step ends.
- A sandbox that is reused for the same profile does not run setup again.

### 5. Add validate steps

Validate steps are the checks you want a change to pass. The agent is told
to run them while it works. After the agent stops, agentic-ci runs them again
and records each result.

```yaml
  validate:
    - {name: lint, kind: lint, run: make lint, timeout: 900}
    - {name: unit, kind: test, run: make unit-test, timeout: 1800}
```

- `kind` is one of `lint`, `build`, `test` or `generated`. Use `generated`
  for checks that generated files are up to date.
- Use the same commands as your CI or your pre-commit hook.
- Keep them fast. A large repo can limit a check to the files or packages
  that differ from the base branch, for example with
  `git diff --name-only "$(git merge-base origin/HEAD HEAD)"`.

The results are a report, not a gate: a failed or timed-out command never
fails the run, and agentic-ci does not stop the pipeline from proposing the
change. A `passed` result is a signal from the sandbox, not proof (see
[What a validate record proves](../sandbox-profiles.md#records)), so keep
your CI and review.

### 6. Declare skips

A skip tells the agent about a check that the sandbox can never run, so it
does not try and does not report it as a failure.

```yaml
  skips:
    - match: "make e2e"
      reason: "Needs a live cluster"
```

A skip cannot hide a validate step that the central profile requires.

### 7. Set environment variables

`env` is for non-secret settings that your tools read.

```yaml
  env:
    CGO_ENABLED: "0"
    HUSKY: "0"
```

Names that could change the agent, git, the proxy or credentials are
refused, for example `PATH`, `HOME` and anything that starts with `GIT_`,
`LD_` or `OPENAI_`, or that contains `token`, `secret`, `key` or `password`.
See [Reserved environment variable names](../sandbox-profiles.md#reserved-environment-variable-names)
for the full list.

### 8. Check the file

Run this at the root of your repo. It parses the overlay with the rules
agentic-ci applies to a repo overlay and prints every warning.

```bash
cat > /tmp/check-overlay.py <<'EOF'
import yaml
from agentic_ci.sandbox_profile import merge_profiles, parse_profile

with open(".agentic-ci/config.yml", encoding="utf-8") as f:
    section = (yaml.safe_load(f) or {}).get("sandbox")
parsed = parse_profile(section, source="overlay")
merged = merge_profiles(None, parsed.profile)
for warning in parsed.warnings + merged.warnings:
    print("WARNING:", warning)
print("toolchains:", dict(merged.profile.toolchains))
print("egress:", list(merged.profile.egress))
print("setup:", [s.name for s in merged.profile.setup])
print("validate:", [v.name for v in merged.profile.validate])
EOF
uvx --from agentic-ci --with pyyaml python /tmp/check-overlay.py
```

A warning means that part of the overlay is dropped. An error means the
whole overlay is ignored.

This checks the overlay alone. The pipeline merges it with its own central
profile for your repo, if there is one, which can override toolchain
versions and `env`, drop steps and skips that repeat central ones, or ignore
the overlay. The pipeline's job log shows the effective result.

To test the commands themselves, run each setup and validate step in a
fresh clone with only the toolchains you listed.

### 9. Merge it

agentic-ci does not read the overlay by itself: the pipeline reads it from a
commit it trusts and passes it on. The autofix bot reads it from the base
branch (the MR/PR target or the default branch), so a change on a feature
branch has no effect until it merges. Check your pipeline's documentation
for what it reads and what it shows you after a run, for example
[autofix's overlay page](https://gitlab.com/redhat/rhel-ai/agentic-ci/autofix/-/blob/main/docs/operations/sandbox-overlay.md).

## Limits

| You need | What to do |
|----------|-----------|
| Another host, such as a Git server or a different registry | Ask the maintainers of the pipeline's central configuration. Overlays cannot open raw hosts. |
| More memory or CPU | Ask the same maintainers. Resources are central only. |
| A toolchain or registry preset that does not exist | Open an issue in [agentic-ci](https://github.com/opendatahub-io/agentic-ci/issues). |
| No overlay for one repo | The central profile can set `overlay: ignore`. |

## Examples

### Python with tox

```yaml
sandbox:
  egress:
    - pypi
  env:
    UV_PYTHON_DOWNLOADS: "never"
  setup:
    - name: tox
      run: uv tool install tox==4.64.9
  validate:
    - {name: tests, kind: test, run: tox -e py312, timeout: 1200}
    - {name: lint, kind: lint, run: tox -e lint}
  skips:
    - match: "e2e/run_*_e2e.sh"
      reason: "Creates real tickets and MRs with live credentials"
```

### Go

```yaml
sandbox:
  toolchains:
    go: auto
    golangci-lint: "2.13.2"
  egress:
    - goproxy
  setup:
    - {name: modules, run: go mod download, timeout: 900}
  validate:
    - {name: lint, kind: lint, run: golangci-lint run ./..., timeout: 900}
    - {name: unit, kind: test, run: go test ./..., timeout: 1800}
  skips:
    - match: "make test-e2e"
      reason: "Needs a live cluster"
```

### Node with pnpm

```yaml
sandbox:
  toolchains:
    node: "22"
    pnpm: auto
  egress:
    - npm
  env:
    HUSKY: "0"
  setup:
    - {name: install, run: pnpm install --frozen-lockfile, timeout: 1800}
  validate:
    - {name: lint, kind: lint, run: pnpm run lint, timeout: 1200}
    - {name: type-check, kind: build, run: pnpm run type-check, timeout: 900}
    - {name: unit, kind: test, run: pnpm run test:unit, timeout: 1800}
  skips:
    - match: "cypress"
      reason: "No browser in the sandbox"
```

## Reference

- [Sandbox Profiles](../sandbox-profiles.md): every field rule, the
  toolchain catalog, the presets and how steps run.
- [Project Configuration](../configuration.md#sandbox-overlay): the other
  files in `.agentic-ci/`.
