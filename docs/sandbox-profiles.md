# Sandbox Profiles

A sandbox profile describes what one target repo needs inside the agent
sandbox: toolchains, network egress, setup and validation commands, checks the
sandbox can never run, environment variables, resources, and paths to drop
before the workdir is copied back. Callers build one with
`agentic_ci.sandbox_profile.parse_profile()` and hand it to the skill runner
as `SkillConfig.sandbox_profile`.

!!! note "What takes effect in this release"
    `resources` and `egress` take effect today: the OpenShell backend sizes
    the sandbox with `resources` and opens the `egress` presets and raw
    endpoints to the agent (see [Egress](#egress)). Every other field is
    validated, merged and carried to the backend, but not yet acted on.
    Toolchain provisioning, in-sandbox setup, validation runs and
    `discard_before_download` land in the following releases.

Sandbox profiles apply only to the OpenShell backend. The Podman and local
backends log a warning and ignore a profile.

## Schema

The same shape is used in JSON (central configuration) and YAML (a repo
overlay):

```yaml
toolchains:            # name -> version
  go: auto
  node: "22"
  shfmt: "3.12.0"
egress:                # preset names; raw host:port:access strings are central-only
  - goproxy
  - npm
setup:                 # commands run inside the sandbox before the agent
  - name: modules
    run: go mod download
    timeout: 900
validate:              # ordered validation commands
  - name: lint
    kind: lint         # lint | build | test | generated
    run: make lint
    timeout: 900
skips:                 # checks the sandbox can never run
  - match: "unshare --net"
    reason: network namespaces are blocked by seccomp
env:                   # non-secret variables for the sandbox
  CGO_ENABLED: "0"
resources:             # central only
  memory: 8Gi
  cpu: "4"
discard_before_download:
  - node_modules
overlay: merge         # central only: merge | ignore
```

Every field is optional. A field set to `null` (for example an empty YAML
key) means its default.

## Validation Rules

| Field | Rule |
|-------|------|
| `toolchains` | Name is one of `buf`, `go`, `golangci-lint`, `helm`, `kustomize`, `node`, `pnpm`, `protoc`, `python`, `shfmt`, `yq`. Version matches `^[0-9]+(\.[0-9]+){0,2}$` or is exactly `auto`. |
| `egress` | Preset names are `pypi`, `npm`, `goproxy`, `github-release-assets`. An entry containing `:` is a raw endpoint, `host:port:access[:protocol[:enforcement[:options]]]`, with no whitespace: `host` is an ASCII host name or IPv4 address, optionally starting with a `*.` wildcard; `port` is 1 to 65535; `access` is `read-only`, `read-write` or `full`; `protocol` is empty, `rest`, `websocket` or `sql`; `enforcement` is empty, `enforce` or `audit`, and needs a protocol; `options` is a comma-separated list of `allow-uninspected-credentials`, `websocket-credential-rewrite`, `request-body-credential-rewrite` and `allowed-ip=IPV4[/BITS]`, and must not be empty when the field is present. |
| `setup`, `validate` | Each step has a `name` of letters, digits, `.`, `_` or `-`, unique within its list; a non-empty `run` string; and a `timeout` in seconds from 1 to 3600 (default 600). A `validate` step also has a `kind`: `lint`, `build`, `test` or `generated`. |
| `skips` | Each entry has non-empty `match` and `reason` strings. |
| `env` | Names match `^[A-Za-z_][A-Za-z0-9_]*$`. See [Reserved environment variable names](#reserved-environment-variable-names) for the names that are rejected. Values are strings; integers are converted with `str()`, and booleans, decimals, null, lists and mappings are rejected. |
| `resources` | `memory` is a positive quantity string: digits with an optional unit of `Ki`, `Mi`, `Gi` or `Ti` (`512Mi`, `8Gi`). `cpu` is a positive string or number such as `"4"`, `"2.5"` or `"500m"`. `gpu` is a non-negative integer. |
| `discard_before_download` | Workdir-relative paths. No absolute paths, no `..` component, no empty string, not the workdir itself (`.`), and not `.git` or anything under it. Paths are normalized (`./dist/` becomes `dist`). |
| `overlay` | `merge` (default) or `ignore`. |

Every list and mapping holds at most 1000 entries (`MAX_ENTRIES`). Every
pattern must match the whole value; a trailing newline is rejected.

### Reserved environment variable names

Profile `env` goes into the same environment as the harness, so names that
would change the harness, its telemetry or credentials, the dynamic loader,
the shell, git, or TLS and proxy settings are rejected. All checks ignore
case.

- Names: `ALL_PROXY`, `BASH_ENV`, `ENV`, `HOME`, `HTTP_PROXY`, `HTTPS_PROXY`,
  `IFS`, `NODE_EXTRA_CA_CERTS`, `NODE_OPTIONS`, `NO_PROXY`, `PATH`,
  `PYTHONHOME`, `PYTHONSTARTUP`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_DIR`,
  `SSL_CERT_FILE`.
- Prefixes: `AGENT_`, `ANTHROPIC_`, `CLAUDE_`, `CLOUD_ML_`, `CODEX_`, `GCP_`,
  `GIT_`, `GOOGLE_`, `LD_`, `OPENAI_`, `OTEL_`, `VERTEX_`.
- Any name containing `token`, `secret`, `key`, `password` or `credential`
  (a substring match, case-insensitive).
- Any name with `pat` (personal access token) as a whole `_`-separated word,
  such as `PAT`, `GH_PAT` or `PAT_FILE`. `GOPATH`, `PYTHONPATH`, `NODE_PATH`
  and `CLASSPATH` are allowed.

!!! tip "Quote versions"
    YAML reads an unquoted `1.20` as the number `1.2`, so the original text
    cannot be recovered. Unquoted decimal versions are rejected; write
    `go: "1.20"`. Unquoted integers such as `node: 22` are accepted.

Errors raise `SandboxProfileError`, a `ValueError` whose message names the
field path and the rule broken, for example
`validate[2].kind: must be one of build, generated, lint, test`. Messages
never include `env` values or `run` strings.

## Central Profiles and Repo Overlays

`parse_profile(data, source=...)` takes `source="central"` for reviewed CI
configuration and `source="overlay"` for a file from the target repo. Central
profiles fail loudly on any problem. An overlay must not be able to break the
run or widen what central configuration allows, so the parser drops these with
a warning instead of raising:

- unknown keys, at the top level or inside a step, skip or resource entry
- unknown toolchains and egress presets
- rejected `env` names
- a step whose name repeats an earlier step in the same list
- raw egress endpoints
- `resources` (runner capacity is a central cost)
- `overlay`

Other overlay problems, such as a bad toolchain version or a step without a
`run`, still raise; the caller decides whether to drop the whole overlay.
Warnings quote untrusted names, so a name cannot add a line to a log or
comment, and at most 50 are returned (`MAX_WARNINGS`), followed by one line
counting the rest.

## Merge Precedence

`merge_profiles(central, overlay)` combines the two:

- Both `None`: returns `None`.
- A central profile with `overlay: ignore` drops the overlay entirely, with
  one warning when an overlay was given.
- Central scalars and map keys win: `toolchains`, `env`, `resources` and
  `overlay`.
- Lists (`setup`, `validate`, `skips`, `egress`, `discard_before_download`)
  are concatenated central first and de-duplicated. Steps are matched by
  `name` and skips by `match`; an overlay step or skip that the central
  profile already defines is dropped with a warning.
- An overlay skip is dropped with a warning when its `match` and the name
  or `run` of a central `validate` step contain one another, ignoring case
  (for example `TEST` or `unit tests` against a step `unit` that runs
  `make test`), so a repo cannot skip validation that central configuration
  requires.
- Overlay egress presets outside `overlay_allowed_presets` (default `pypi`,
  `npm`, `goproxy`) are dropped with a warning.
- An overlay built directly instead of by `parse_profile()` is held to the
  same rules: unknown toolchains, bad toolchain versions and rejected `env`
  names are dropped with a warning.
- An overlay with no central profile is merged into a default envelope:
  `overlay: merge`, only allowed presets, no resources and no raw egress.

The result is a `MergedProfile` with the effective profile and a tuple of
warnings. It never raises for an overlay that `parse_profile(...,
source="overlay")` accepted.

## Serialization

`profile_to_dict()` returns the shape `parse_profile()` accepts, so a central
profile round-trips. `profile_hash()` is the sha256 hex digest of that dict as
canonical JSON (`sort_keys=True`, compact separators).

## Resources

When the OpenShell backend receives a profile with `resources`, each of
`memory`, `cpu` and `gpu` comes from the profile unless the caller passed that
value to the backend explicitly; explicit values win. The backend logs one line
saying where each value came from. As with explicit values, resources apply
only when the sandbox is created; see
[Sandbox Resources](backends/openshell.md#sandbox-resources).

## Egress

With a profile, the OpenShell backend builds the sandbox's network policy from
the built-in defaults, the endpoints the harness's auth mode needs, and then
the profile's egress: the endpoints of each preset open in the `agent` phase,
followed by the profile's raw endpoints (central only), de-duplicated. These
rules are bound to the agent binaries, like the defaults. The repo's
`.agentic-ci/openshell-policy.yml` is ignored when a profile is set, so only
reviewed configuration opens egress; the log says `Policy source: sandbox
profile`, with ` (repo policy file ignored)` appended when the repo has that
file. An explicit `--policy` flag still applies.
Without a profile the policy is exactly what it was before profiles existed.

The profile's hash is recorded in the sandbox identity file, so a changed
profile recreates the sandbox instead of reusing one with the old egress. A
reused sandbox is switched to the `agent` phase before anything runs, in case
an earlier run stopped in `setup` or `validate`. Resources the profile set
are not reported as "not applied" on reuse, since the hash guarantees them.

| Preset | Endpoints (all `443:read-only:rest:enforce`) | Phases |
|--------|----------------------------------------------|--------|
| `pypi` | `pypi.org`, `files.pythonhosted.org` | setup, validate, agent |
| `npm` | `registry.npmjs.org` | setup, validate, agent |
| `goproxy` | `proxy.golang.org`, `sum.golang.org`, `storage.googleapis.com` | setup, validate, agent |
| `github-release-assets` | `release-assets.githubusercontent.com`, `objects.githubusercontent.com`, `raw.githubusercontent.com` | setup, validate, agent |

The endpoint lists live in `agentic_ci.backends.openshell.policy.EGRESS_PRESETS`.
Raw endpoints apply to all three phases.

Presets are read-only at L7. `rest` makes the OpenShell proxy terminate TLS
and inspect every HTTP request, and `read-only` then allows only `GET`, `HEAD`
and `OPTIONS`; any other method gets a `403` from the proxy (a JSON body with
`"error": "policy_denied"`) and never reaches the registry. `enforce` is
required, because OpenShell defaults to `audit`, which only logs the denied
request and forwards it. Without a protocol an endpoint is an L4 `CONNECT`
tunnel, where `read-only` blocks nothing: `npm publish` or an upload to any
Cloud Storage bucket would get through. Reads still go out, so data sent in a
`GET` request (a path or query string) remains an accepted residual risk. The
proxy's CA is trusted through the variables OpenShell sets in the sandbox
(`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS` and others).

A preset host always has exactly one endpoint, the preset's, in every phase.
Any other endpoint on port `443` whose host overlaps a preset host the
profile opens in that phase (the same host, or a wildcard such as
`*.googleapis.com` that covers it) is replaced in its place by the preset's
endpoint, whatever its access or protocol. That covers the defaults
(`pypi.org:443:read-only` and `files.pythonhosted.org:443:read-only`, so with
the `pypi` preset the agent also reaches PyPI through the L7 read-only rule),
raw endpoints and `--policy` entries. A replaced raw or `--policy` endpoint is
counted in a `WARNING: N egress endpoint(s) for an egress preset host
replaced` line; a replaced wildcard no longer opens its other hosts, so list
those explicitly. Without this the phases would differ: `openshell policy
update` folds a second endpoint for a host into the preset's rule and keeps
the preset's access for the agent, while a shim phase policy has one rule per
endpoint, so a raw `registry.npmjs.org:443:full` would reopen `PUT` to the
shim. Endpoints on another port, and every other raw endpoint, keep the
access and protocol central configuration gives them. Without a profile
nothing is replaced.

### Phases and the setup shim

A sandbox moves through three egress phases: `setup` (before the agent),
`agent`, and `validate` (after the agent). For `setup` and `validate`, the
backend adds one rule per endpoint of the presets open in that phase plus the
raw endpoints, bound only to the setup shim
`/usr/local/bin/agentic-ci-sandbox-setup`, and parks the agent's rules: each
agent binary path becomes `/proc/agentic-ci-parked/<path>`, which no process
can match, so a step that runs `codex` (or another agent binary) under the
shim gets no agent egress. The `agent` phase strips every shim rule and
restores the agent's rules unchanged, credential bindings included. The shim
only gets the profile's presets and raw endpoints, never the default forge
rules, LLM endpoints or the OTel collector (a preset such as `pypi` can still
name a host that the defaults also allow): a raw endpoint whose host overlaps an auth endpoint or the collector (wildcards
included), or that carries a credential option such as
`allow-uninspected-credentials`, is left out of the shim's rules and only
their count is logged. Each switch replaces the whole policy with `openshell
policy set` and closes every open proxied connection, so it happens only while
nothing runs in the sandbox.

One gap is accepted: the OpenShell `openai` and `anthropic` provider profiles
bind the API host to `/usr/bin/curl` and `/usr/local/bin/curl` and inject the
real key into those requests. Those rules are composed by the server, are not
part of `policy get --base`, and so stay live in every phase: setup and
validate code cannot read the key but can spend it through curl, as the agent
already can.

The shim is a small static binary in the OpenShell sandbox images. It runs its
arguments as a child process in a new process group, waits, forwards
`SIGTERM`, `SIGINT`, `SIGHUP` and `SIGQUIT` to that whole group (so a step
killed on timeout takes its own children with it), and exits with the child's
status (`128+N` for a signal).
OpenShell matches a connection by the caller's executable and its parent
chain, so a command started through the shim, and everything it starts, gets
the shim's rules; a bare process, or one the shim leaves behind after it
exits, does not. Running setup and validate steps through the shim lands in a
following release.
