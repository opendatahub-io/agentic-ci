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
- **Expected behavior**: Run start logs `Reasoning effort: high`; the synthetic root span carries `agent.reasoning_effort` (and `agent.subagent_reasoning_effort` for Codex); the agent environment has `AGENT_REASONING_EFFORT=high`; Codex `codex.conversation_starts` events report the effort.
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

### OpenShell cleanup fails with `no such table: objects`
- **Likely cause**: The local gateway used `sqlite::memory:` and lost its schema when SQLite replaced the connection during concurrent sandbox cleanup. The gateway now uses a temporary file-backed database so replacement connections share the same schema.
- **Where to look**: `backends/openshell/gateway.py` database URL creation and cleanup, gateway logs around `DeleteSandbox` and compute-driver watch events

### `openshell policy set` rejects credential binding: "uses L4-only; configure L7 inspection or set allow_uninspected_credentials"
- **Likely cause**: OpenShell v0.0.112+ validates that credentialed endpoints use L7 inspection. GCP endpoints use L4 CONNECT tunneling (required for Vertex AI gRPC streaming), so setting `credential_binding.provider` without `allow_uninspected_credentials: true` is now rejected. Fixed by adding `allow_uninspected_credentials: true` alongside the credential binding.
- **Where to look**: `backends/openshell/policy.py:build_credential_binding_patch()`

### Routed Codex run falls back: `classifier exited with code -15`
- **Symptom**: `run_routed_skill` on the OpenShell backend logs `stream completed (rc=-15) but verdict file .../verdict.json missing; keeping original exit code` right after the classifier wrote `_run/route.json`, then `Model routing fell back to <default>: classifier exited with code -15`.
- **Likely cause**: Codex does not exit within the 10s grace period after its final stream event, so the backend terminates it (`-15`). The backend only promotes a terminated-after-completion run to 0 when its completion file exists. The classifier run now passes `completion_file=route.json` to the session, so the check looks for the route file rather than the skill verdict. If this reappears, check that `_router` in `skill.py` still wraps `session.run` with `completion_file`.
- **Where to look**: `skill.py` `_AgentSession.run(completion_file=...)` and `_router`, `backend.py` `_resolve_exit_code()`, `routing.py` `classify()`

### Codex OTel data missing or `openshell sandbox download` returns a corrupt tar on OpenShell rhaiv.11
- **Symptom**: Codex runs finish but the OTLP artifact has no Codex response (autofix e2e: `Native Codex OTel response missing or invalid (model=missing, tokens=0)`), or the workdir download fails with `failed to extract tar archive from sandbox ... numeric field did not have utf-8 text when getting cksum`.
- **Likely cause**: agentic-ci 0.3.62 moved to OpenShell `v0.0.116-rhaiv.11` (supervisor/sandbox split, `network=none` workload). Codex exports OTLP over plain HTTP to `host.openshell.internal`, which the new network path appears not to deliver (unconfirmed), and the supervisor can crash mid-run (NVIDIA/OpenShell#3396), corrupting the download stream. The move was reverted to `rhaiv.1` until both are fixed; re-land it only after the autofix e2e OTel checks pass.
- **Where to look**: `images/ci/Containerfile.openshell` `OPENSHELL_IMAGE_TAG`, `backends/openshell/__init__.py` OTel endpoint lines, supervisor log for `transparent_tcp_mapping_denied`

### OpenCode image build fails with `KeyError: 'repo'`
- **Likely cause**: A marketplace plugin uses a `git-subdir` source with `url` and `path` instead of the legacy GitHub `repo` field. The OpenCode compatibility installer must resolve both source formats and search for skills relative to the configured subdirectory.
- **Where to look**: `plugins.py:install_opencode_skills()`, the generated marketplace entry
