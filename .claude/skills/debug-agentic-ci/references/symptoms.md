# Agentic-CI Symptom Catalog

Known failure patterns from this repo's history. Update this file when fixing bugs or adding features that change failure modes.

## Container backend

### Container exits with code 137 (OOM or SIGKILL)
- **Likely cause**: Entrypoint sleep passthrough caused the container to receive SIGKILL on timeout instead of graceful shutdown. Fixed by removing sleep passthrough.
- **Where to look**: `images/runner/shared/entrypoint.sh`, `backends/podman.py` timeout handling

### Container entrypoint bypassed
- **Likely cause**: PodmanBackend was using `--entrypoint` override. Fixed by stopping the bypass so credential setup in entrypoint.sh runs.
- **Where to look**: `backends/podman.py` container launch args

### Container config dirs overwritten with /sandbox paths
- **Likely cause**: Podman backend was injecting OpenShell-style `/sandbox` paths into config dirs. Fixed to use container-appropriate paths.
- **Where to look**: `backends/podman.py` config dir setup, `harness.py` path resolution

### AGENT_ENABLED_PLUGINS not working in OpenShell
- **Likely cause**: Plugin env var wasn't being passed through to the sandbox environment. Fixed by explicitly forwarding it.
- **Where to look**: `backends/openshell/sandbox.py` env var injection, `harness.py`

## Skill engine

### Verdict file missing but agent exited 0
- **Likely cause**: Agent was SIGKILL'd (timeout) after producing output but before writing verdict. The completion validator now checks verdict file existence before promoting SIGKILL exit to success.
- **Where to look**: `skill.py:run_skill()` verdict loading, completion validator logic

### Routed skill ran on the default model (source=fallback)
- **Likely cause**: The classifier run failed: non-zero exit, `_run/route.json` missing or not valid JSON, or a `difficulty` outside low/medium/high. On Claude Code the `--max-turns` cap can expire before the agent writes the file. Routing is fail-open by design, so the run continues on the default model with a warning.
- **Expected behavior**: `RouteDecision.source == "fallback"`, `tier is None`, and the `skill.routed` event in `_run/claude-otel.jsonl` records the fallback. The skill result itself is unaffected.
- **Where to look**: `routing.py:classify()` / `load_route()`, `_run/classifier-output.txt`, `skill.py:run_routed_skill()`, `classifier_max_turns`

### Agents run at low reasoning effort (shallow reviews, tiny reasoning token counts)
- **Likely cause**: No effort reached the agent CLI, so the model default applied (Codex defaults to `low` on `gpt-5.6-sol`, RHAIFIRST-649). Fixed by passing the registry `default_effort` (`high`) on every run and `agents.default_subagent_reasoning_effort` for Codex sub-agents. Recurs if an env override sets `none` or a low value.
- **Expected behavior**: Run start logs `Reasoning effort: high`; the synthetic root span carries `agent.reasoning_effort` (and `agent.subagent_reasoning_effort` for Codex); Codex `codex.conversation_starts` events report the effort.
- **Where to look**: `harness.py:resolve_efforts()`, `models.py:MODEL_REGISTRY` (`default_effort`, `subagent_effort`), `CLAUDE_REASONING_EFFORT` / `OPENCODE_REASONING_EFFORT` / `CODEX_REASONING_EFFORT` / `CODEX_SUBAGENT_REASONING_EFFORT`, `--effort`

### Run fails before the agent starts with "Unsupported ... effort"
- **Likely cause**: `--effort` or a `*_REASONING_EFFORT` env var holds a value outside the registry `efforts` set. Validation is deliberate: Codex accepts unknown values silently and then runs at the model default.
- **Where to look**: `harness.py:build_effort_args()`, `models.py:MODEL_REGISTRY` (`efforts`)

### Routed run fails immediately with an unknown flag or variant
- **Likely cause**: The tier's effort value is not accepted by the agent CLI in the runner image (Claude `--effort`, OpenCode `--variant`, Codex `-c model_reasoning_effort=`). Effort values are validated against a per-harness allow-list at config time, but the image's CLI version decides what actually works.
- **Where to look**: `models.py:MODEL_REGISTRY` (`efforts`, tiers), `harness.py:effort_args()` flag shape, `SkillConfig.model_tiers`, runner image CLI version pins under `images/runner/`

### Verdict rejected: string where array expected
- **Likely cause**: LLM returns single values instead of arrays. Fixed by coercing string verdict list fields to arrays.
- **Where to look**: `verdict.py` coercion logic, `skill.py` verdict validation

## Forge (MR/PR operations)

### GitHub comment filtering returns wrong comments
- **Likely cause**: GitHub API pagination or comment type filtering was incorrect. Fixed to properly filter by comment type.
- **Where to look**: `forge.py` GitHub comment methods, API pagination

### GitHub CI status detection wrong
- **Likely cause**: Check runs vs commit statuses weren't both queried. Fixed to check both GitHub status APIs.
- **Where to look**: `forge.py` CI status detection methods

### Artifact files left in commits
- **Likely cause**: `strip_committed_files()` didn't exist. Added to remove skill artifacts from git commits before push.
- **Where to look**: `gates.py:strip_committed_files()`, post-gate execution in `skill.py`

## Credentials and auth

### GCP project ID not resolved
- **Likely cause**: Harness project resolution didn't fall back to `GCP_PROJECT_ID` env var when gcloud config was empty.
- **Where to look**: `harness.py` project resolution, `cli.py` credential setup

### OTEL collector not receiving data
- **Likely cause**: For OpenShell backend, OTEL host networking wasn't configured. Fixed to set up OTEL endpoint forwarding.
- **Where to look**: `otel.py` collector setup, `backends/openshell/` network config

### Generic event export fails after a workflow completes
- **Likely cause**: Producer event was not JSON-compatible, violated privacy or size limits, the runner-owned log root was unsafe, or the OTLP collector was unavailable.
- **Expected behavior**: `agentic_ci.telemetry` raises a transport error. The caller owns failure policy and must preserve its workflow result when export is best effort.
- **Where to look**: `telemetry.py:validate_event()`, `telemetry.py:emit_event()`, and the producer's export boundary.

### Codex starts but plugins or OTEL data are missing
- **Likely cause**: Codex was launched with `--ignore-user-config`, which suppresses plugin state and user-level OTel configuration, or the per-run `otel.*` exporter overrides were not passed.
- **Where to look**: `harness.py` Codex arguments, `plugins.py`, backend `otel_endpoint` argument wiring

### Codex OpenShell setup creates an Anthropic provider
- **Likely cause**: Codex was classified as generic `api-key` auth and OpenShell selected its Anthropic provider. Codex must use the `openai` auth mode and the OpenAI network endpoints.
- **Where to look**: `harness.py` auth mode, `backends/openshell/provider.py`, `backends/openshell/policy.py`

### OpenShell harness cannot reach its model API, or can reach another harness's API
- **Likely cause**: The sandbox policy was resolved without the harness auth mode. Authentication endpoints are scoped to `vertex`, `api-key`, or `openai`; only common forge and package endpoints are shared.
- **Where to look**: `backends/openshell/policy.py`, `backends/openshell/sandbox.py` auth mode wiring

### Codex exits before local or Podman execution with "credentials not found"
- **Likely cause**: None of `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, or `OPENAI_API_KEY` is available. Local runs may instead use `$CODEX_HOME/auth.json`; Podman runs require a forwarded environment credential.
- **Where to look**: `CodexHarness.validate_credentials()`, backend `setup()`, CI secret injection, and `CODEX_HOME`

## Jira client

### Markdown formatting lost in ADF roundtrip
- **Likely cause**: `adf_to_text()` was stripping markdown formatting. Fixed to preserve it during conversion.
- **Where to look**: `jira.py:adf_to_text()`

## Gates

### Sensitive-files gate blocks files in directories named `secrets/`
- **Likely cause**: `check_sensitive_files` was matching patterns against both the filename and the full path. Python's `fnmatch` treats `*` as matching path separators, so the `*secret*` blocklist pattern matched directory components like `secrets/`. Fixed by restricting matching to the filename only (`os.path.basename`).
- **Where to look**: `gates.py:check_sensitive_files()`, fnmatch pattern matching

## Container images

### AGENTS.md not found in container
- **Likely cause**: COPY paths in Containerfiles didn't match the repo layout after restructuring.
- **Where to look**: `images/runner/shared/Containerfile.base`, COPY directives

### OpenShell sandbox auto-attaches provider
- **Likely cause**: Default provider attachment behavior interfered with custom credential injection. Fixed by preventing auto-attachment.
- **Where to look**: `backends/openshell/sandbox.py` provider config

### agentic-ci fails to run in OpenShell image (Python missing or too old)
- **Likely cause**: The Hummingbird agentic images do not include Python. The sandbox images install the repository's `python3` package, which is required for the `uv pip install --system` step and for running `agentic-ci` inside the sandbox.
- **Where to look**: `images/ci/Containerfile.openshell`, `images/runner/claude-code/Containerfile.openshell`, `images/runner/opencode/Containerfile.openshell`, `images/runner/codex/Containerfile.openshell` python setup

### OpenShell sandbox build fails after an agentic base image switches to Hummingbird
- **Likely cause**: Hardened Hummingbird harness images intentionally omit a package manager. Keep the agentic image as the final runtime and mount the matching digest-pinned Hummingbird builder recorded by `io.openshell.hummingbird.builder-image`. Use the builder's `dnf` and repository configuration to add packages, then remove its caches. Do not install Hummingbird RPMs into an unrelated UBI final stage because the RPM databases, loaders, libraries, and signing policies do not match.
- **Package names**: The Node.js 26 runtime and npm are already present as `nodejs26` and `nodejs26-npm`. Install missing Python as `python3`; `uv` does not require a separate pip package.
- **Where to look**: `images/runner/claude-code/Containerfile.openshell`, `images/runner/opencode/Containerfile.openshell`, `images/runner/codex/Containerfile.openshell`

### Hummingbird OpenShell sandbox exits during provisioning
- **Symptom**: Direct image checks pass, but `openshell sandbox create` reports `ContainerExited: Container exited with code 1` before the sandbox becomes ready.
- **Likely cause**: The Hummingbird runtime lacks `/usr/bin/nsenter`. The OpenShell Podman supervisor requires `nsenter` to configure the workload network namespace and exits during provisioning when it is unavailable. Install `util-linux-core` through the matching Hummingbird builder and verify `nsenter --version` in image tests.
- **Where to look**: `images/runner/*/Containerfile.openshell`, `tests/e2e/e2e-openshell-sandbox.sh`, the supervisor's network namespace setup

### Hummingbird harness exits 126 with `/usr/local/sbin/<harness>: Permission denied`
- **Likely cause**: The Claude Hummingbird image exposes a symlink from `/usr/local/bin/claude` into `/opt`. OpenShell's process identity and executable-path handling rejects this symlink even though the launcher works in an ordinary container. Materialize a hard link in the Claude sandbox image, and keep both `/usr/local/bin/<harness>` and `/usr/local/sbin/<harness>` in the network policy aliases.
- **Where to look**: `images/runner/claude-code/Containerfile.openshell`, `backends/openshell/sandbox.py`, the sandbox image's `PATH` and `/usr/local/sbin` link

### CI image build cannot find the pinned ACLI RPM
- **Likely cause**: Atlassian removed the pinned ACLI version from its RPM repository. Query the live repository metadata and update `ACLI_VERSION` in both CI Containerfiles to the currently published version.
- **Where to look**: `images/ci/Containerfile.podman`, `images/ci/Containerfile.openshell`, Atlassian ACLI RPM repository metadata

### `openshell policy update --wait` times out with "Timeout waiting for policy version 2 to load"
- **Likely cause**: The sandbox was created with a short-lived canonical main process (e.g. `-- true`). OpenShell v0.0.111+ (#2726) treats the trailing argv as the sandbox's canonical process; when it exits, the supervisor shuts down before it can acknowledge policy v2. Fixed by using `--detach -- sleep infinity` so the main process stays alive while policy updates and agent commands run via `sandbox exec`.
- **Where to look**: `backends/openshell/sandbox.py:create()` canonical process args

### Gateway never becomes healthy: gateway.toml rejected (`unknown field `compute_drivers`` or `unsupported gateway config version 1`)
- **Symptom**: `agentic-ci run --backend openshell` fails with `RuntimeError: Gateway did not become healthy within 60s`; the gateway log shows either `TOML parse error ... unknown field `compute_drivers`, expected one of ... `compute_driver` ...` or `unsupported gateway config version 1; this build requires version 2; migrate legacy fields to the version 2 schema`.
- **Likely cause**: OpenShell v0.0.116-rhaiv.8+ (upstream sync in opendatahub-io/openshell#44) moved gateway.toml to schema version 2: `[openshell] version = 2`, a singular `compute_driver` string instead of the `compute_drivers` list, and driver options only under `[openshell.drivers.<name>]`. Version 1 files are rejected outright. agentic-ci renders `version = 2` and `compute_driver = "podman"`.
- **Where to look**: `backends/openshell/gateway.py` `_GATEWAY_TOML`, the gateway log under `~/.local/state/openshell/gateway-*.log`, the upstream migration guide at https://docs.nvidia.com/openshell/latest/reference/gateway-config#migrate-to-schema-version-2

### Provider creation fails: `provider profile 'google-cloud' not found; import a matching profile before using this provider type`
- **Symptom**: `openshell provider create --type google-cloud|openai|anthropic` exits 1 during `agentic-ci run --backend openshell`, right after the gateway becomes healthy.
- **Likely cause**: OpenShell v0.0.116-rhaiv.8+ removed the built-in provider profiles; a fresh gateway serves an empty catalog. agentic-ci imports the vendored profiles from `backends/openshell/profiles/` through `provider.ensure_profiles()` before creating the provider. If the error persists, check that `openshell provider list-profiles --global` shows the profile, that the YAML still lints (`openshell provider profile lint -f <file>`), and that the profile ID matches the `--type` value.
- **Where to look**: `backends/openshell/provider.py` `ensure_profiles()` and `PROFILE_IDS`, `backends/openshell/profiles/*.yaml`, the upstream `providers/` directory in opendatahub-io/openshell for schema changes

### `openshell sandbox create` times out: `ConfigurationInvalid: Effective configuration could not be activated; replace the policy or repair attached providers`
- **Symptom**: `sandbox provisioning timed out after 300s` with that status; both the supervisor and workload containers are up; `openshell policy get ci` says `no active policy configured`. Every E2E test then waits the full 300s, which looks like a hung job.
- **Likely cause**: The supervisor log (`/var/log/openshell.<date>.log` inside the `openshell-supervisor-<id>` container) shows `Server returned no policy; attempting local discovery` followed by `Image policy is invalid; replace the sandbox policy to repair configuration`. The Hummingbird agentic images ship `/etc/openshell/policy.yaml` in a non-OpenShell schema (`sandbox:`/`permissions:` keys). OpenShell v0.0.116-rhaiv.8+ parses the discovered image policy strictly and rejects the sandbox instead of falling back to its restrictive default. agentic-ci now always passes `--policy` with `policy.BASE_POLICY` at create time, which skips image discovery entirely.
- **Where to look**: `backends/openshell/sandbox.py:create()` `--policy` handling, `backends/openshell/policy.py:BASE_POLICY`, `podman exec openshell-supervisor-<id> cat /var/log/openshell.*.log` on the CI host, `openshell sandbox get ci -o json` `configuration_admission`

### Sandbox creation pulls `ghcr.io/nvidia/openshell/sandbox` or fails with a missing `openshell-sandbox` binary
- **Symptom**: After bumping `OPENSHELL_IMAGE_TAG` to v0.0.116-rhaiv.8 or later, `openshell sandbox create` tries to pull `ghcr.io/nvidia/openshell/sandbox:<tag>` (unreachable from CI), or the supervisor container starts but the workload never runs `/openshell-sandbox`.
- **Likely cause**: OpenShell split the supervisor image into `odh-openshell-supervisor` (runs in its own container) and `odh-openshell-sandbox` (static binary mounted into the workload). The gateway's podman driver needs both `supervisor_image` and `sandbox_runtime_image` in `gateway.toml`; without the second it falls back to the upstream ghcr.io default. agentic-ci renders both from `OPENSHELL_SUPERVISOR_IMAGE` and `OPENSHELL_SANDBOX_RUNTIME_IMAGE`, which the CI image sets from `OPENSHELL_IMAGE_TAG`. Conversely, tags before v0.0.116-rhaiv.8 have no `odh-openshell-sandbox` image, so `bump-versions.py` only selects tags published for all four repos.
- **Where to look**: `images/ci/Containerfile.openshell` ENV lines, `backends/openshell/gateway.py:_render_podman_driver_section()`, `scripts/bump-versions.py:_quay_latest_openshell()`, `tests/e2e/e2e-openshell-sandbox.sh` image resolution

### OpenShell cleanup fails with `no such table: objects`
- **Likely cause**: The local gateway used `sqlite::memory:` and lost its schema when SQLite replaced the connection during concurrent sandbox cleanup. The gateway now uses a temporary file-backed database so replacement connections share the same schema.
- **Where to look**: `backends/openshell/gateway.py` database URL creation and cleanup, gateway logs around `DeleteSandbox` and compute-driver watch events

### `openshell policy update` rejects the sandbox policy: "network endpoint ambiguity validation failed ... overlap on port(s) 443 with conflicting metadata"
- **Likely cause**: The sandbox policy declares a host that the attached provider profile already contributes as an L7 `rest` layer (`_provider_ci_gcp` / `_provider_ci_vertex`): `aiplatform.googleapis.com`, `api.anthropic.com`, or `api.openai.com`. Since OpenShell v0.0.116-rhaiv.8+ agentic-ci leaves those hosts out of `policy.AUTH_ENDPOINTS`; a repo `.agentic-ci/openshell-policy.yml` that re-adds them trips the same validation.
- **Where to look**: `backends/openshell/policy.py:AUTH_ENDPOINTS`, the repo policy file, `openshell policy get ci --full -o json`

### Claude Code on Vertex fails inside OpenShell: `API Error: Could not refresh access token` or `Could not load the default credentials`
- **Symptom**: The sandbox reaches `Ready`, the run starts, and the first API call fails. The workload container log shows `sandbox network notification denied ... Connection refused` for `127.0.0.1:8174`.
- **Likely cause**: OpenShell v0.0.116-rhaiv.8+ removed the GCE metadata emulator (`openshell-sandbox` no longer serves `127.0.0.1:8174`), so SDK-side ADC discovery cannot mint a token. agentic-ci now uses the `google-vertex-ai` profile: the gateway injects `GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN` / `GOOGLE_VERTEX_AI_TOKEN` as a placeholder and the Claude env script sets `CLAUDE_CODE_SKIP_VERTEX_AUTH=1`, `ANTHROPIC_AUTH_TOKEN=<placeholder>`, and `ANTHROPIC_VERTEX_BASE_URL` (host plus `/v1`), so the request carries the placeholder as a bearer token and the supervisor proxy swaps in the real one. If this reappears, check that the env script still exports those three variables and that the profile's `binaries` include the harness launcher.
- **Where to look**: `harness.py` `ClaudeCodeHarness.build_env_script_lines()`, `backends/openshell/provider.py` `_create_gcp_provider_*`, `backends/openshell/profiles/google-vertex-ai.yaml`, supervisor log `ALLOWED POST ... [policy:_provider_ci_gcp engine:l7]`

### OpenCode on Vertex fails inside OpenShell: `Was there a typo in the url or port?` or `Could not load the default credentials`
- **Symptom**: The OpenCode run errors before any request reaches `aiplatform`; the supervisor log shows no `ALLOWED POST` for the model.
- **Likely cause**: OpenCode's `@ai-sdk/google-vertex` mints a token through `google-auth-library` before every request. agentic-ci satisfies it with an `external_account` ADC whose `token_url` is `http://127.0.0.1:8175/token`, served by `agentic-ci vertex-token-stub` started from the env script. "typo in the url or port" means the stub is not listening: the sandbox image's agentic-ci predates the `vertex-token-stub` subcommand, `AGENTIC_CI_VERTEX_TOKEN` was empty (no provider placeholder injected) so the stub refused to start, or the sandbox base policy blocks `/tmp`. Check `/tmp/.agentic-ci-vertex-stub.log` inside the sandbox.
- **Where to look**: `harness.py` `_OPENSHELL_VERTEX_ADC_STUB_LINES`, `vertex_token_stub.py`, `cli.py` `vertex-token-stub` dispatch, the sandbox image's installed agentic-ci version

### Every host fails to resolve inside the OpenShell sandbox (`Could not resolve host`)
- **Symptom**: `curl`/agent requests fail with DNS errors even for allowed endpoints; `/etc/resolv.conf` in the workload is empty while `ss -ltn` shows a listener on `127.0.0.53:53`.
- **Likely cause**: With the supervisor/sandbox split the workload runs with `network=none` and the `openshell-sandbox` binary serves policy DNS on `127.0.0.53:53`. Podman writes no `resolv.conf` for `network=none`, the Hummingbird base ships an empty one, and the sandbox runs as uid 1001 so it cannot rewrite the root-owned file. The sandbox images now `COPY images/runner/shared/resolv.conf` (`nameserver 127.0.0.53`); a `RUN` that writes the file does not persist because buildah bind-mounts `resolv.conf` during builds.
- **Where to look**: `images/runner/*/Containerfile.openshell`, `images/runner/shared/resolv.conf`, `tests/e2e/e2e-openshell-sandbox.sh` resolv.conf assertions

### OpenCode image build fails with `KeyError: 'repo'`
- **Likely cause**: A marketplace plugin uses a `git-subdir` source with `url` and `path` instead of the legacy GitHub `repo` field. The OpenCode compatibility installer must resolve both source formats and search for skills relative to the configured subdirectory.
- **Where to look**: `plugins.py:install_opencode_skills()`, the generated marketplace entry
