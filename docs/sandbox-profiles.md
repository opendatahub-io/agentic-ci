# Sandbox Profiles

A sandbox profile describes what one target repo needs inside the agent
sandbox: toolchains, network egress, setup and validation commands, checks the
sandbox can never run, environment variables, resources, and paths to drop
before the workdir is copied back. Callers build one with
`agentic_ci.sandbox_profile.parse_profile()` and hand it to the skill runner
as `SkillConfig.sandbox_profile`.

!!! note "What takes effect in this release"
    Every field takes effect in the OpenShell backend: it sizes the sandbox
    with `resources`, opens the `egress` presets and raw endpoints (see
    [Egress](#egress)), provisions the `toolchains` (see
    [Toolchains](#toolchains)), runs the `setup` steps in the sandbox before
    the agent and the `validate` commands after it, records the declared
    `skips`, exports `env`, removes the `discard_before_download` paths and
    tells the agent all of it in `ENVIRONMENT.md` (see
    [Setup, validation and records](#setup-validation-and-records)). The
    host-side `setup:` of `.agentic-ci/config.yml` no longer runs by default:
    it is deprecated, runs only with the local opt-in `--allow-host-setup`
    (`SkillConfig.allow_host_setup`), which is refused in CI, and is not
    containment (see [Setup Steps](configuration.md#setup-steps-deprecated-host-path)).
    A profile's `setup` steps are the replacement.

Sandbox profiles apply only to the OpenShell backend. The Podman and local
backends log a warning and ignore a profile (with further warnings counting
the toolchains they do not provision and the setup steps and validate
commands they do not run). Profile steps never run on the host.

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
| `toolchains` | Name is an entry of the [toolchain catalog](#catalog): `buf`, `go`, `golangci-lint`, `helm`, `kustomize`, `node`, `pnpm`, `protoc`, `python`, `rust`, `shfmt`, `yq`. Version matches `^[0-9]{1,9}(\.[0-9]{1,9}){0,2}$` or is exactly `auto`; which forms resolve depends on the tool (see [Version forms](#version-forms)). |
| `egress` | Preset names are `pypi`, `npm`, `goproxy`, `crates`, `github-release-assets`. An entry containing `:` is a raw endpoint, `host:port:access[:protocol[:enforcement[:options]]]`, with no whitespace: `host` is an ASCII host name or IPv4 address, optionally starting with a `*.` wildcard; `port` is 1 to 65535; `access` is `read-only`, `read-write` or `full`; `protocol` is empty, `rest`, `websocket` or `sql`; `enforcement` is empty, `enforce` or `audit`, and needs a protocol; `options` is a comma-separated list of `allow-uninspected-credentials`, `websocket-credential-rewrite`, `request-body-credential-rewrite` and `allowed-ip=IPV4[/BITS]`, and must not be empty when the field is present. |
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
would change the harness (Claude Code, Codex, OpenCode), agentic-ci or
OpenShell, their telemetry or credentials, the dynamic loader, the shell,
git, the XDG directories the harnesses keep their config and credentials
in, or TLS and proxy settings are rejected. All checks ignore case.

- Names: `ALL_PROXY`, `BASH_ENV`, `CURL_CA_BUNDLE`, `DENO_CERT`, `ENV`,
  `HOME`, `HTTP_PROXY`, `HTTPS_PROXY`, `IFS`, `METADATA_SERVER_DETECTION`,
  `NODE_EXTRA_CA_CERTS`, `NODE_OPTIONS`, `NO_PROXY`, `PATH`, `PYTHONHOME`,
  `PYTHONSTARTUP`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_DIR`, `SSL_CERT_FILE`,
  `TERM`, `USER`.
- Prefixes: `AGENT_`, `AGENTIC_CI_`, `ANTHROPIC_`, `CLAUDE_`, `CLOUD_ML_`,
  `CODEX_`, `GCP_`, `GIT_`, `GOOGLE_`, `LD_`, `OPENAI_`, `OPENCODE_`,
  `OPENSHELL_`, `OTEL_`, `VERTEX_`, `XDG_`.
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
  `npm`, `goproxy`, `crates`) are dropped with a warning.
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
| `crates` | `index.crates.io`, `static.crates.io` | setup, validate, agent |
| `github-release-assets` | `release-assets.githubusercontent.com`, `objects.githubusercontent.com`, `raw.githubusercontent.com` | setup, validate, agent |

The endpoint lists live in `agentic_ci.backends.openshell.policy.EGRESS_PRESETS`.
Raw endpoints apply to all three phases.

`crates` covers cargo's sparse index (`index.crates.io`, the default
protocol from cargo 1.70; the rust toolchain sets it for older releases too)
and the crate downloads its `config.json` points to (`static.crates.io`).
The crates.io API host is left out, since only search and publish use it.
Git dependencies need their forge host, which only central configuration can
open.

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

The `npm` preset's endpoint also allows an encoded slash
(`allow_encoded_slash`): npm and pnpm fetch a scoped package's metadata as
`/@scope%2fname`, and OpenShell's L7 check rejects `%2F` by default, which
would deny every scoped package. `openshell policy update --add-endpoint`
cannot set that option, so agentic-ci sets it in the YAML policy it applies
with `openshell policy set`: on every phase switch, and right after creating
a sandbox whose endpoints include the npm host, for profiles without setup
or validate steps. The hosts that need it are
`agentic_ci.backends.openshell.policy.ENCODED_SLASH_HOSTS`.

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
name a host that the defaults also allow): a raw endpoint whose host overlaps
an agent-only host, or that carries a credential option such as
`allow-uninspected-credentials`, is left out of the shim's rules and only
their count is logged. The agent-only hosts are the hosts of every auth
mode's endpoints, every endpoint host of the provider profiles agentic-ci
vendors (`src/agentic_ci/backends/openshell/profiles/`, read at import, which
fails on a malformed profile), `oauth2.googleapis.com`,
`*.aiplatform.googleapis.com` and the OTel collector
(`host.openshell.internal`). Wildcards on either side are compared as
patterns, so `*.googleapis.com` overlaps `oauth2.googleapis.com`, and the
Vertex profile's `*-aiplatform.googleapis.com` keeps
`us-central1-aiplatform.googleapis.com` from the shim. The vendored profiles
declare `api.openai.com`, `api.anthropic.com` and the Vertex AI hosts
(`*-aiplatform.googleapis.com`, `aiplatform.googleapis.com`,
`aiplatform.us.rep.googleapis.com`, `aiplatform.eu.rep.googleapis.com`),
which no longer appear in any auth mode's endpoints. Each switch replaces
the whole policy with `openshell policy set` and closes every open proxied
connection, so it happens only while nothing runs in the sandbox.

The shim lets setup and validate reach preset hosts without widening the
agent's egress. It is an extra layer, not isolation: it is not a boundary
against the agent, and any sandbox process that ran it while shim rules are
live would get the shim's egress. OpenShell has no per-exec kill (a process an
exec leaves running survives the exec) and no per-exec network profile, so the
protections are, on every switch:

- **Shim rules exist only in `setup` and `validate`.** The `agent` phase
  strips them, and it must be in effect before the agent starts.
- **The agent's rules are parked** while shim rules are live (see above).
- **Leftover processes are killed.** Before any phase opens, a scan run as the
  sandbox user inside the sandbox SIGKILLs every process of that user except
  the sandbox's main process (`sleep infinity`, identified by the PID and start
  time recorded right after `sandbox create` and saved with the sandbox
  identity) and the scan itself: the agent's background jobs and `setsid`
  daemons, earlier setup or validate steps, and what they started. Processes
  are killed one by one, never by process group, since exec'd commands share
  the supervisor's group. If anything survives, or the recorded main process
  is not running (a stale record: nothing is killed then), the switch fails
  and the phase does not run; a reused sandbox that cannot be switched to
  `agent` is recreated. The scan, like every internal exec, runs with
  `--no-login-shell`, so an agent-written `~/.bash_profile` cannot replace
  it or start anything after it. The kill also runs on the way into `agent`,
  so every phase starts with only the main process: a setup leftover could
  otherwise write the workdir while the agent works, and on a reused sandbox
  the previous run's processes would run next to the new agent. Setup
  therefore cannot start a service for the agent or for validate.
- **The credential provider is detached** while `setup` or `validate` is
  open, for openai, api-key (Anthropic) and vertex auth. The rule OpenShell
  composes from agentic-ci's provider profile binds the API hosts to the agent
  binaries and is not part of `policy get --base`, so parking cannot remove
  it, and a step could otherwise spend the credential by running an agent
  binary under the shim. A running sandbox keeps injecting for up to about
  10 s after `openshell sandbox provider detach`, so the switch first makes
  sure a fresh exec gets the provider placeholder (attaching the provider if
  not), detaches (`--wait`, which returns once the supervisor has installed
  the change), and then checks (for up to 30 s) until a fresh exec no longer
  gets it: the supervisor swaps a sandbox's environment and its credential
  bindings together, and after the swap no placeholder resolves, including
  one issued before the detach. That probe covers injection only; the
  provider's composed rule leaves with the policy the supervisor reloads in
  the same poll, and the phase's `policy set --wait`, issued after the
  detach, confirms it whenever the policy changes. Detaching closes the
  provider's route to the API hosts, not the credential: with api-key auth
  the agent already holds the real key (the env script exports it until
  RHAI-3011 is fixed) and could leave it in the workdir for setup or validate
  code it also wrote to send out through the shim's egress. With openai and
  vertex auth the agent only sees the placeholder. The `agent` switch
  attaches the provider again and waits for the placeholder, so the agent
  can authenticate; this also repairs a reused sandbox a run left detached.
  The placeholder does not change across the detach and the attach, so one
  the agent stored earlier (Codex's `auth.json`) still resolves. The `oauth`
  mode has no provider.

While the agent phase is in effect, the agent can still spend its own key
through an agent binary wrapper such as `codex sandbox -- curl` (see
[API Key](backends/openshell.md#api-key-direct-anthropic-api)); detaching
covers only setup and validate.

The shim is a small static binary in the OpenShell sandbox images. It runs its
arguments as a child process in a new process group, waits, forwards
`SIGTERM`, `SIGINT`, `SIGHUP` and `SIGQUIT` to that whole group (so a step
killed on timeout takes its own children with it), and exits with the child's
status (`128+N` for a signal).
OpenShell matches a connection by the caller's executable and its parent
chain, so a command started through the shim, and everything it starts, gets
the shim's rules; a bare process, or one the shim leaves behind after it
exits, does not, and a process an earlier exec left running is killed before
the phase opens anyway. The profile's setup steps and validate commands run
through the shim (see [Setup, validation and records](#setup-validation-and-records)).

## Toolchains

The OpenShell backend provisions each toolchain of the profile right after
the workdir upload, before any setup step (see
[Setup order](backends/openshell.md#setup-order)). The work is split so that
nothing from the repo or an archive ever runs on the host:

1. **Host: resolve.** The requested version becomes an exact release. For
   `auto`, the host reads repo files at the workdir root as data (see
   [`auto` sources](#auto-sources)).
2. **Host: download and verify.** The host fetches the tool's official
   checksum source and the archive, over HTTPS, from the entry's fixed
   hosts only (a redirect to any other host is refused), with size and
   time limits (32 MiB and 120 s for indexes and checksum files, 512 MiB
   and 900 s for archives). For a GitHub release, every github.com hop,
   redirects included, must stay under the entry's own
   `/<owner>/<repo>/releases/` path, so a renamed or transferred repo
   cannot supply the archive or its checksum, and GitHub's asset hosts are
   accepted only as the last hop of such a redirect. A connection error or
   a 5xx status before the body starts is retried twice (after 2 s and
   5 s, within the time limit). The archive is kept only if its digest
   matches the official one; a mismatch, or a missing checksum, fails
   closed and nothing is installed.
3. **Host: cache.** Verified archives are stored in
   `~/.cache/agentic-ci/toolchains/<sha256>` (`AGENTIC_CI_TOOLCHAIN_CACHE`
   moves it), written to a temporary file and renamed, so a cache hit skips
   the download. A cached archive is hashed again before use.
4. **Sandbox: extract.** The archive is uploaded (900 s limit; a failed
   upload is removed again) and extracted by the sandbox image's
   `/usr/bin/python3` (`tarfile` with gzip or xz, `zipfile`; the images ship
   python3, tar, gzip and xz but no unzip) into
   `/sandbox/.local/toolchains/<name>-<version>`. The upload is opened once
   (not through a symlink, a regular file only) and copied into a fresh
   private directory while it is hashed; only that copy, with the sha256
   the host verified, is extracted. Every member is checked before anything
   is extracted: no absolute path, no `..`, no member under a symlink, no
   duplicate, no device, FIFO or other special file, and every symlink or
   hard link must resolve inside the target directory. The extraction goes
   to a temporary directory that is renamed into place with a marker file
   recording the sha256. For `rust`, only the listed components of the
   standalone installer are extracted (links in them are refused) and their
   files are merged into one tree, the layout the installer's `install.sh`
   produces, without running it; a file two components both provide is
   refused.

A reused sandbox skips a toolchain only when the host's record of that
sandbox (the saved sandbox identity lists every toolchain directory it
installed, with its sha256) and the marker agree; such a toolchain is not
downloaded, and it is not even read from the host cache (pnpm, whose
official digest is sha512, is read from the cache to learn its sha256).
Anything else is installed again, which replaces the directory. The marker
only records what was installed: it is not an integrity check of the
installed files. `/sandbox/.local` is writable by the agent, so a directory
an earlier agent planted (even with a matching marker) is never trusted,
but files an earlier agent run changed inside a recorded toolchain are not
detected either. A changed profile, toolchains included, recreates the
sandbox and with it the record.

A toolchain that cannot be resolved, downloaded, verified or extracted is
recorded as `failed` and skipped; the other toolchains and the run go on.
Results go to the job log and, for skill runs, to `_run/toolchains.json` on
the host, one entry per toolchain:

```json
[
  {"name": "go", "requested": "auto", "resolved": "1.26.5",
   "sha256": "9fa5...", "status": "installed", "reason": ""}
]
```

`status` is `installed`, `present` (a reused sandbox already had it, by
the host's record and the marker) or `failed`, with `reason` saying why.
`run()` writes the file again after the workdir download, since the
workdir's `_run` comes back from the sandbox too and the host's record must
win. Reasons are fixed messages plus validated
versions and file names; they never contain repo file content, exception
text or command output.

The download hosts (`go.dev`, `dl.google.com`, `nodejs.org`,
`static.rust-lang.org`, `registry.npmjs.org` as a download source, GitHub
release downloads, `get.helm.sh`) are contacted by the host only. Toolchains add nothing to the
sandbox policy: the sandbox reaches a registry only through an
[egress preset](#egress).

### Catalog

| Name | Archive (host) | Checksum source | `PATH` entry |
|------|----------------|-----------------|--------------|
| `go` | `dl.google.com/go/go<v>.linux-<arch>.tar.gz` | `go.dev/dl/?mode=json&include=all` (`sha256`) | `go/bin` |
| `node` | `nodejs.org/dist/v<v>/node-v<v>-linux-<arch>.tar.gz` | `SHASUMS256.txt` of the release | `node-v<v>-linux-<arch>/bin` |
| `pnpm` | `registry.npmjs.org/pnpm/-/pnpm-<v>.tgz` | npm `dist.integrity` (sha512) | launchers `pnpm`, `pnpx` (run with `node`) |
| `shfmt` | GitHub release of `mvdan/sh` | `sha256sums.txt` | the binary |
| `golangci-lint` | GitHub release | `golangci-lint-<v>-checksums.txt` | `golangci-lint-<v>-linux-<arch>` |
| `helm` | `get.helm.sh/helm-v<v>-linux-<arch>.tar.gz` | `.sha256sum` next to it | `linux-<arch>` |
| `yq` | GitHub release of `mikefarah/yq` | `checksums` (the `SHA-256` column named by `checksums_hashes_order`) | the binary |
| `kustomize` | GitHub release (`kustomize/v<v>` tag) | `checksums.txt` | the archive root |
| `buf` | GitHub release | `sha256.txt` | `buf/bin` |
| `protoc` | GitHub release of `protocolbuffers/protobuf` (zip) | `tool_integrity.bzl` from v36.0; a vendored table before | `bin` |
| `python` | GitHub release of `astral-sh/python-build-standalone` (the `install_only` builds uv uses) | `SHA256SUMS` of the newest release | `python/bin` |
| `rust` | `static.rust-lang.org/dist/rust-<v>-<arch>-unknown-linux-gnu.tar.xz` (about 210 MB; the `rustc`, `rust-std`, `cargo`, `clippy-preview` and `rustfmt-preview` components, about 650 MB installed) | `.sha256` next to it | `bin` |

protoc publishes no checksum manifest before v36.0, so agentic-ci vendors the
sha256 of 28.3, 29.5, 30.2, 31.1, 32.1, 33.0, 33.6, 34.1 and 35.1
(`agentic_ci.toolchains.PROTOC_SHA256`); any other version before 36.0 is
refused. Every vendored sha256 was computed from the release download and
cross-checked against a second source: 28.3 to 33.0 against the
`aspect-build/toolchains_protoc` table, 33.6, 34.1 and 35.1 against the
integrity table inside protobuf's own release source archive, whose digest
the Bazel Central Registry pins. protoc versions are exactly `MAJOR.MINOR`
(`33.0`, not `33.0.0`). The machine is the host's (`x86_64` or `aarch64`), since OpenShell
runs the sandbox natively.

### Version forms

- **Exact** (`1.26.5`, `22.19.0`, protoc `33.0`): every tool.
- **Partial** (`22`, `1.26`): resolves to the newest matching release for
  `go` (stable releases in the go.dev JSON), `node` (`index.json`, releases
  with a Linux build for the machine), `pnpm` (the npm registry, no
  prereleases), `python` (the builds in the newest python-build-standalone
  release) and `rust` (`MAJOR.MINOR` only: the `[pkg.rust]` version of
  `channel-rust-<MAJOR.MINOR>.toml`, that minor release's newest patch). For
  every other tool a partial version is an error that asks for an exact
  version.
- **`auto`**: see below; the version read is then resolved like a requested
  one, so `go 1.26` in go.mod gives the newest 1.26.x.

A partial `python` version resolves against the newest
python-build-standalone release, which carries one patch release per minor
version. An exact version it lacks (`3.12.3`, say, once a newer 3.12 ships)
resolves through `agentic_ci.toolchains.PYTHON_BUILD_TAGS`, a vendored table
of the release that holds each CPython 3.9 and later version's newest build
(taken from uv's download metadata); its sha256 still comes from that
release's `SHA256SUMS`. A version released after agentic-ci and already
superseded in the newest release is an error until the table is updated.

### `auto` sources

Files are read from the workdir root, at most 256 KiB each (1 MiB for
`package.json`), as UTF-8 text. A symlink is followed only while it stays
inside the workdir, and anything but a regular file is refused. Every value
read must match the version regex; nothing is ever executed.

| Tool | Sources, first found wins |
|------|---------------------------|
| `go` | `go.mod`: the higher of the `toolchain goX.Y.Z` and `go` lines, as the go command needs both (`toolchain default` counts as absent) |
| `node` | `.nvmrc`, `.node-version` (a leading `v` is dropped), `package.json` `engines.node`, `.tool-versions` (`nodejs` or `node`) |
| `pnpm` | `package.json` `packageManager`: `pnpm@X.Y.Z` or `pnpm@X.Y.Z+sha512.<hex>`; with the hash, the registry's sha512 must match it too |
| `python` | `.python-version`, `.tool-versions` (`python`) |
| `rust` | `rust-toolchain` (TOML, or the legacy bare channel line), `rust-toolchain.toml` (`channel` in `[toolchain]`), `.tool-versions` (`rust`) |

`engines.node` accepts only simple forms: an exact or partial version, or
`^`, `~` or `>=` followed by a major (`>=22` resolves the newest 22.x).
Anything else, such as `>=22.18.0 <23` or `lts/*` in `.nvmrc`, is an error
naming the file. A rust `channel` must be a version: `stable`, `beta`,
`nightly`, a dated or target-suffixed channel and a `path` toolchain are
errors, and `components` and `targets` are ignored (the toolchain always
has the five components above, for the host's target only). `auto` for any
other tool is an error.

### Where things live and the environment

| Path | What |
|------|------|
| `/sandbox/.local/toolchains/<name>-<version>/` | extracted toolchain, with `.agentic-ci-toolchain.json` (the marker) |
| `/sandbox/.local/gopath` | `GOPATH` (its `bin` is on `PATH`) |
| `/sandbox/.cache/go-mod`, `/sandbox/.cache/go-build` | `GOMODCACHE`, `GOCACHE` |
| `/sandbox/.cache/npm` | `npm_config_cache` |
| `/sandbox/.cache/pnpm-store` | `npm_config_store_dir` (pnpm 10 and earlier), `pnpm_config_store_dir` (pnpm 11) |
| `/sandbox/.local/cargo` | `CARGO_HOME` (registry cache and `cargo install` binaries; its `bin` is on `PATH`) |

All of it is outside the workdir, so none of it is downloaded back or
committed. The env script sourced before the agent prepends each
provisioned toolchain's `PATH` entries and exports `GOTOOLCHAIN=local`,
`GOPATH`, `GOMODCACHE` and `GOCACHE` (with go), `npm_config_cache` (with
node or pnpm), the pnpm store variables (with pnpm), and `CARGO_HOME` and
`CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse` (with rust). `CARGO_TARGET_DIR`
is not set, so builds write `target/` in the workdir; list it in
`discard_before_download` to keep it out of the download. A toolchain that
failed exports nothing. `GOTOOLCHAIN=local` makes a go.mod that needs a
newer Go fail (`go.mod requires go >= ...; GOTOOLCHAIN=local`) instead of
downloading that toolchain. `OpenShellBackend.toolchain_env` holds the same
variables, which the setup steps and validate commands get too.

## Setup, validation and records

The OpenShell backend runs the profile's `setup` steps and `validate`
commands inside the sandbox, never on the CI host (see
[Setup order](backends/openshell.md#setup-order) and
[Run order](backends/openshell.md#run-order)).

### How a step runs

Each setup step and validate command runs as

```bash
openshell sandbox exec --name ci --no-tty --no-login-shell -- \
  /usr/bin/python3 -I -S -c <step wrapper> <spec> \
  /usr/local/bin/agentic-ci-sandbox-setup /usr/bin/bash -c "<run>"
```

The step wrapper (`agentic_ci.backends.openshell.steps`) changes to the
workdir (`/sandbox/<workdir name>`), builds the step's environment from
scratch and runs the shim, which runs `bash -c "<run>"` in its own process
group, so the phase's egress reaches the step and everything it starts. The
environment is:

| Variable | Value |
|----------|-------|
| `PATH` | the provisioned toolchains' directories, then `/sandbox/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin` |
| `HOME`, `LANG` | `/sandbox`, `C.UTF-8` |
| toolchain variables | `GOTOOLCHAIN`, `GOPATH`, `GOMODCACHE`, `GOCACHE`, `npm_config_cache`, the pnpm store (see above) |
| profile `env` | as set (wins over a toolchain variable of the same name) |
| inherited | only OpenShell's CA bundle variables (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `DENO_CERT`, `SSL_CERT_DIR`), proxy variables if a runtime sets them, `OPENSHELL_SANDBOX`, `TERM` and `USER` |

Nothing else reaches the step: not the LLM env script (never sourced), not
the provider placeholder, not a forge, Jira or LLM credential of the host.
The internal exec runs with `--no-login-shell`, so an agent-written
`~/.bash_profile` does not run before a validate command either.

Each step has its own `timeout` (default 600 s, at most 3600 s), enforced in
the sandbox: when it passes, the wrapper sends `SIGTERM` to the shim (which
forwards it to the step's process group) and to every process below it, then
`SIGKILL` 10 s later, and the step is recorded as `timeout` (exit 124). The
host gives up on an exec that does not return a minute after that.

No step process outlives its step. The wrapper is a child subreaper
(`PR_SET_CHILD_SUBREAPER`), so a background job or a double-forked daemon the
step starts is reparented to it rather than to the sandbox's init; as soon as
the shim exits (the step passed, failed or timed out), the wrapper sends
`SIGKILL` to every process still below it and reaps them. A step that leaves
a daemon holding its output therefore ends when its shell does, and a
process that ignores `SIGTERM` does not survive the timeout.

### Setup steps

When the sandbox is created, after the toolchains are provisioned, the
backend switches to the `setup` phase (kill the leftovers, detach the
provider, apply the setup policy), runs every setup step in order and
switches back to the `agent` phase before anything else happens. A step
that fails or times out is recorded and the next step still runs; the agent
is told in `ENVIRONMENT.md`. When the setup phase cannot be opened, no step
runs and each is recorded as `not_run`; a failed switch back to `agent`
fails `setup()`, because the agent must never start with setup egress open.

A reused sandbox (same identity, so the same profile) does not run its setup
steps again: their effects are still in the sandbox, and the results saved
with the sandbox identity when it was created are reported again. A profile
without setup steps switches no phase in `setup()`.

### Validate commands

After the agent exits and the OTel flush, and before the workdir is
downloaded, the backend:

1. switches to the `validate` phase, which kills every process left in the
   sandbox and then deletes the harness credential files: Codex's
   `$CODEX_HOME/auth.json` (`/sandbox/.codex/auth.json`), Claude Code's
   `/sandbox/.claude/.credentials.json`, OpenCode's
   `/sandbox/.local/share/opencode/auth.json` and the env script
   `/tmp/.agentic-ci-env.sh` if it is still there. The wipe runs after the
   kill, so a process the agent left cannot write a file back, and it must
   be confirmed: otherwise no validate command runs. Then the provider is
   detached and the validate policy applied: the agent's rules parked, the
   shim bound to the presets open in the validate phase, and never an LLM
   or forge host;
2. runs each validate command in order, as above;
3. switches back to the `agent` phase, so a reused sandbox starts clean. A
   failure here is logged and remembered: the next `setup()` repairs or
   recreates the sandbox, and a `run()` on the same backend without a
   `setup()` first switches to the `agent` phase before it writes the env
   script, and raises (no agent starts) while that fails.

A copy of a credential the agent made elsewhere during its run is not
covered; that is the agent phase's existing exposure. Validation never
changes the run's exit code: a validate phase that cannot be opened records
every command as `not_run`. A model-routing classifier run
(`run_routed_skill`) changes no code, so it skips validation
(`SkillSession.run(..., validate=False)` sets `Backend.validate_after_run`);
the skill run that follows validates.

### discard_before_download

After validation, each `discard_before_download` path is moved out of the
workdir inside the sandbox, to `/sandbox/.agentic-ci/discarded`, before the
download, so a large `node_modules` is never copied back. The next `run()` on
the same sandbox (the skill run after a classifier run, or a retry) moves it
back before its agent starts, so what the setup steps installed stays
available; a path whose place was taken in the meantime is not restored, and
the stash is deleted either way. Paths are workdir-relative; `..`, absolute
paths, the workdir itself and `.git` are refused when the profile is parsed
and again in the sandbox. Every directory on the way is opened without
following symlinks, so a symlink anywhere in the path makes it `refused`
instead of reaching outside the workdir, and a symlink as the last component
is removed itself, never its target. Without a usable stash directory (a
symlink planted there) paths are deleted instead. Failures are recorded, not
fatal.

### Records

The host writes the records into the run directory (`<workdir>/_run` for a
skill run) and writes them again after the workdir download, which brings
the sandbox's copy of `_run` back: a record the agent forged there is
replaced. Every record name in the table below that the run did not write
is removed after the download, whatever the profile (with no profile too),
so a forged `sandbox-validation.json` for a profile without validate
commands, say, never survives. A `_run` the agent replaced with a symlink or
a file is replaced by a directory first; nothing is written or removed
through the link. A write error is logged, never raised.

| File | Written for | Entries |
|------|-------------|---------|
| `sandbox-setup.json` | a profile with setup steps | `{name, status, rc, seconds, tail[, reason]}` |
| `sandbox-validation.json` | a profile with validate commands or skips, in a run that validated (not a classifier run) | `{name, kind, status, rc, seconds, tail[, reason]}` |
| `sandbox-discard.json` | a profile with discard paths | `{path, status}` |
| `toolchains.json` | a profile with toolchains | see [Toolchains](#toolchains) |

`status` is `passed`, `failed` (non-zero `rc`), `timeout`, `error` (could
not be started) or `not_run` (the phase could not be opened; `reason` says
so). Declared skips are entries of `sandbox-validation.json` with `kind:
"skip"`, `status: "skipped"`, the skip's `match` as `name`, its `reason` and
`rc: null`, so consumers can show what did not run. Discard statuses are
`removed`, `absent`, `refused`, `failed` or `error`.

`tail` holds at most the last 50 lines of the step's combined output, each
cut to 400 characters, with ANSI escapes and control characters removed and
secrets redacted (`agentic_ci.redact`): `Authorization`, `Cookie` and API key
headers, `Bearer` and `Basic` credentials, credentials in URLs, well-known
token shapes (OpenAI, Anthropic, GitHub, GitLab, npm, Slack, AWS, Google,
JWTs, OpenShell placeholders), the value of `NAME=value` and `"name":
"value"` pairs whose name looks like a credential, and the value of every
variable the host holds whose name looks like one. Redaction is a filter for
accidental leaks, not a guarantee. It runs in time linear in the output (a
credential-like name is matched only where a run of name characters starts,
with at most 128 characters before and after its credential word).

!!! warning "What a validate record proves"
    The host ran the command and recorded its exit status and output; it
    does not prove the command was the one the repo intended or ran
    unmodified tools. The validate commands run in the workdir the agent
    changed, with the toolchains under `/sandbox/.local/toolchains` and
    `/sandbox/.local/bin` first on `PATH`, all of which the agent can write
    during its run (a replaced `node` or an edited test can make a command
    pass). Consumers such as a merge gate should treat `passed` as a signal
    from the sandbox, not as host-verified evidence, and pair it with CI or
    review.

### ENVIRONMENT.md

For every profile, before the agent starts, the backend writes
`/sandbox/.agentic-ci/ENVIRONMENT.md` and `environment.json`: the
provisioned toolchains, the egress presets open to the agent, the setup
steps and their results (with the last output lines of a failed step), the
validate commands to run, the declared skips and the names of the profile's
`env` variables. The directory is outside the workdir, so neither file is
downloaded back or committed; a symlink or file the agent left at that path
is replaced, never written through. The sandbox `AGENTS.md` tells the agent
to read `ENVIRONMENT.md` first and to treat it as authoritative.

Repo-controlled strings stay data: step names (letters, digits, `.`, `_` and
`-` only) appear in inline code, and commands, skip text and output tails only
inside fenced code blocks whose fence is longer than any backtick run they
contain, under a note that they are data, not instructions. Lists are capped
at 50 entries and strings at a few thousand characters. Writing the files is
best effort: a failure is logged and the run goes on.

### env

The profile's `env` is exported by the agent's env script too, so the
agent, the setup steps and the validate commands see the same values. In
the env script the toolchain variables and then the profile's `env` come
last, after every command the script runs (`agentic-ci enable-plugins`), so
they reach only the agent. A profile name the script already exports, or
that the caller passes in the backend's `extra_env` (`container_env`), is
not exported (`WARNING: N profile env variable(s) not exported to the
agent`, with the names in the job log): agentic-ci's own values always win,
also for a name the [reserved list](#reserved-environment-variable-names)
misses.
