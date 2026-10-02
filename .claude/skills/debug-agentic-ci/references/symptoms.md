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

### `.agentic-ci/config.yml` setup steps no longer run ("Host setup is disabled: N setup step(s) ... not run")
- **Likely cause**: Host setup is off by default (RHAI-2619). `Backend._run_setup_steps()` runs the repo's `setup:` on the host only when the backend was built with `allow_host_setup=True` (`--allow-host-setup` on `setup`/`run`, or `SkillConfig.allow_host_setup`); otherwise it logs only the step count. The opt-in is deprecated and meant for local use. Move the steps to a sandbox profile's `setup` (OpenShell, runs in the sandbox). No log line while `config.yml` has setup steps means `AGENTIC_CI_SKIP_SETUP=1` is set, which wins over the flag (autofix sets it).
- **Related**: `--allow-host-setup is refused in CI` (CLI exit 2, or `HostSetupRefusedError` from `create_backend`) means `CI`, `GITLAB_CI` or `GITHUB_ACTIONS` is set to something other than empty, `0` or `false`; the opt-in is never allowed in CI. A step that worked before but now fails with the flag may depend on the host env it no longer gets (only `PATH=/usr/local/bin:/usr/bin:/bin`, a temporary `HOME` and git config vars pointing at `/dev/null`), on a background process it started (its process group is killed after the step), or on a `.git/config` or hook change it made (the workdir's git control files are restored after the steps; changes are logged as `restored: ...`). This is cleanup, not containment.
- **Where to look**: `backend.py` (`_run_setup_steps`, `_run_host_setup_step`, `running_in_ci`), `backends/__init__.py` (`create_backend` passes the flag only when `is True`), `skill.py` (`allow_host_setup` threading), `cli.py`

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

### Codex uses fallback model metadata and every `spawn_agent` call fails
- **Symptom**: The raw Codex stream (`agent-output.txt`, `_run/classifier-output.txt`, or `codex exec --json` stdout) has an `item.completed` item of type `error` reading ``Model metadata for `gpt-6-sol` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.``, and every `spawn_agent` call fails with ``Reasoning effort `high` is not supported for model `gpt-6-sol`. Supported reasoning efforts:`` followed by an empty list. Tool output is also capped at 10,000 bytes. The CI log shows none of this, because `CodexStreamProcessor` does not print `error` or `collab_tool_call` items.
- **Likely cause**: The Codex CLI in the runner image is older than the model it runs, so it has no bundled metadata for it and falls back to generic metadata with no supported reasoning efforts. In RHAIFIRST-665 the codex sandbox image shipped Codex 0.153.4 while the Codex default was `gpt-6-sol`, which Codex knows from 0.157.0; fixed by moving the sandbox to the 0.159.0 agentic codex base image and the Podman runner (then on 0.150.1) to Codex 0.159.0. When a Codex default or tier model changes, check the pinned Codex knows it: in the image, `OPENAI_API_KEY=dummy codex exec --json --skip-git-repo-check -m <model> hi` must not print the warning (the auth error that follows is expected).
- **Regression guard**: The routed Codex sections of `tests/e2e/e2e-codex-runner.sh` and `tests/e2e/e2e-openshell-sandbox.sh` fail when the raw streams of the classifier (default model) or the skill run (routed tier) carry the warning.
- **Where to look**: `codex --version` in the image, `ARG CODEX_BASE_IMAGE` in `images/runner/codex/Containerfile.openshell`, `ARG CODEX_VERSION` in `images/runner/codex/Containerfile` (Podman runner), the Codex entry in `models.py:MODEL_REGISTRY`, `CODEX_MODEL`

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

### Trusted MR/PR feedback dropped, or outsider feedback kept
- **Symptom**: A consumer that filters with `filter_trusted_threads()` / `filter_trusted_comments()` ignores a maintainer's review comment, or keeps a comment from someone the consumer did not expect to trust. GitHub trust is based on `author_association` (OWNER, MEMBER, COLLABORATOR), not repository permission, so an organization member or collaborator with only Read or Triage access is still trusted; a consumer that needs write-level trust must check permissions itself.
- **Likely cause**: Trust comes from per-comment fields (RHAI-2629). GitHub uses `author_association`; only `OWNER`, `MEMBER` and `COLLABORATOR` are trusted, and GitHub reports a private org member as `CONTRIBUTOR` or `NONE` when the token cannot see private membership (GitHub App without the Members read permission). GitLab uses `author_access_level` from `members/all/:user_id`, trusted at Developer (30) or above; `0` means not a member (404) or a membership whose `state` is not `active` (for example `awaiting` or `blocked`) and `None` means the lookup failed (a `HTTP ... resolving access level` warning) or the note had no author id. Both count as untrusted, by design. Levels are cached per `GitLabForge` instance, so a role change is seen only by a new client. Threads keep trusted replies even when the starter is untrusted, and are dropped only when no trusted comment remains.
- **Where to look**: `forge/__init__.py` `is_trusted_comment()` / `TRUSTED_GITHUB_ASSOCIATIONS` / `MIN_TRUSTED_GITLAB_ACCESS_LEVEL`, `forge/github.py` `review_comments()` / `general_comments()`, `forge/gitlab.py` `member_access_level()` / `_note_comment()`; `agentic-ci forge mr-comments <URL>` shows the per-comment fields

### Artifact files left in commits
- **Likely cause**: `strip_committed_files()` didn't exist. Added to remove skill artifacts from git commits before push.
- **Where to look**: `gates.py:strip_committed_files()`, post-gate execution in `skill.py`

### Host git ran a hook, fsmonitor or filter the agent configured
- **Symptom**: Host-side git after the agent (artifact stripping `git rm --cached` / `git commit --amend`, `git push`) runs a script from the workdir, or the job log shows `Agent changed git control files in <repo>/.git; restored host copy of: ...`.
- **Likely cause**: The OpenShell workdir download and the Podman bind mount both let the agent write the host repo's `.git`. Before RHAI-2618 nothing restored it, so `core.hooksPath`, `core.fsmonitor`, filter drivers and `.git/hooks` the agent set ran on the host with the job's secrets. The backends now snapshot `config`, `config.worktree`, `commondir`, `hooks/` and `info/` before the agent can write and restore them afterwards. Podman stops the container (`podman stop --time 0`) before restoring, so a process the agent daemonized cannot rewrite `.git` while host git or `--post-gates` run; the next run in the same process records the host copy again, so host edits to `.git/config` between runs are kept, and restarts it (log line `Podman container restarted`). A `podman stop failed (...)` line means the container was removed instead. An agent-created `.git` in a workdir that had none is deleted. The warning line means an agent tried it and was reverted. A run that fails with `GitControlTamperError` means the agent replaced `.git` itself, or the host copy could not be written back and `.git` was moved aside (the workdir then has no repository, by design). A `Moved <repo>/.git aside` warning after a podman error means the container could be neither stopped nor removed, so `.git` was discarded rather than trusted. Restores never read or walk agent-written control paths before renaming them into `.git/agentic-ci-untrusted-*`; a leftover directory with that name is inert and only means its deletion failed. Git config the agent sets for its own use no longer survives the run, by design. The Local backend has no sandbox and does not restore.
- **Where to look**: `git.py` `snapshot_git_control()` / `restore_git_control()` / `GIT_CONTROL_PATHS`, `backend.py` `_snapshot_host_git()` / `_restore_host_git()`, `backends/openshell/__init__.py` `run()`, `backends/podman.py` `setup()` / `run()` / `_park()` / `_unpark()` / `_halt_then_restore()` / `stop()`

### Push rejected because the remote branch moved (stale lease)
- **Symptom**: `push_branch()` returns False and the job log shows `git push rejected: origin/<branch> no longer matches the lease (expected <sha | no remote branch | the remote-tracking ref>); someone else updated it, not overwriting or retrying`, with git's `! [rejected] ... (stale info)` line.
- **Likely cause**: Working as designed. Someone (usually a human reviewer) pushed to the branch after the caller recorded its lease, so the push was refused instead of overwriting their commit. Stale-lease rejections are never retried, even when stderr also matches a transient pattern. Since RHAI-3020, callers that record the remote branch commit before an agent runs pass it as `expected_remote_sha=<full sha>` (or `RemoteLease.ABSENT` for a branch that must not exist yet); git then checks the remote against that value instead of the local remote-tracking ref, which an agent that can write `.git` could move or remap through `remote.<name>.fetch`. The default (`RemoteLease.TRACKING`) is still the bare `--force-with-lease`. An `invalid expected_remote_sha` error means the caller passed something other than a full 40 or 64 hex SHA or a `RemoteLease` value (abbreviated SHAs are refused because git resolves them in the local repository, where a ref spelled like the abbreviation wins); `an explicit branch is required` means it gave a lease without `branch=`, and `needs a short branch name` means it passed `refs/...` as the branch. All three push nothing. With an explicit lease the refspec is `refs/heads/<branch>:refs/heads/<branch>`, so a missing local branch fails with `src refspec ... does not match any` instead of pushing a same-named tag. A `cannot parse expected object name` error means a 64 hex SHA was given for a SHA-1 repository or vice versa.
- **Where to look**: `git.py` `push_branch()` / `RemoteLease` / `_is_stale_lease_rejection()`, and where the caller records the expected SHA

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

### Nested `codex exec` runs use the wrong model or effort, or their tokens are missing from OTel
- **Symptom**: The top-level command carries `-m <model>` and `-c model_reasoning_effort=high`, but `agent_output.txt` shows nested runs (for example the `autofix-resolve` implement and review agents) headed with another model and `reasoning effort: none`, and `claude-otel.jsonl` has no spans or token counts for them (RHAI-2624).
- **Likely cause**: Nested runs do not see the top-level `-m`/`-c` flags; they only read `$CODEX_HOME/config.toml`. On OpenShell, `CodexHarness.build_args()` writes a `# BEGIN agentic-ci run settings` block there before every run. It is missing when the run was not on OpenShell (local and Podman leave `config.toml` alone), when the file already set the key outside the block (the wrapper then prints `agentic-ci: config.toml already sets <key>; nested codex runs keep it` on stderr), or when the nested call passes its own `-m`/`-c` flags. If the wrapper exits with `agentic-ci: could not write .../config.toml`, `CODEX_HOME` is not writable in the sandbox.
- **Where to look**: `harness.py` `CodexHarness.run_settings_toml()`, `_write_settings_script()` and `_CODEX_SETTINGS_AWK`; `cat $CODEX_HOME/config.toml` in the sandbox; the nested command lines in the agent output.

### Codex OpenShell setup creates an Anthropic provider
- **Likely cause**: Codex was classified as generic `api-key` auth and OpenShell selected its Anthropic provider. Codex must use the `openai` auth mode and the OpenAI network endpoints.
- **Where to look**: `harness.py` auth mode, `backends/openshell/provider.py`, `backends/openshell/policy.py`

### OpenShell harness cannot reach its model API, or can reach another harness's API
- **Likely cause**: The sandbox policy was resolved without the harness auth mode. Authentication endpoints are scoped to `vertex`, `api-key`, `oauth`, or `openai`; only common forge and package endpoints are shared.
- **Where to look**: `backends/openshell/policy.py`, `backends/openshell/sandbox.py` auth mode wiring

### OpenShell e2e fails "plain curl gets no OpenAI key" or "plain curl gets no Vertex token": "the proxy logged it DENIED by policy"
- **Symptom**: The Codex or Claude Code (Vertex) section of `tests/e2e/e2e-openshell-sandbox.sh` prints `before=N after=N` and the proxy lines for `/usr/bin/curl`.
- **Likely cause**: If the curl output is an HTTP body (for example a 401 `invalid_api_key` JSON, or Google's `UNAUTHENTICATED`) and the proxy logged `ALLOWED /usr/bin/curl(...) -> api.openai.com:443` (or an aiplatform host), a rule admits curl to the API again, most likely an OpenShell example profile (`openai`, `google-vertex-ai` with curl added) in place of `agentic-ci-openai` or `agentic-ci-google-vertex-ai`. If curl failed and the proxy logged a `DENIED` line with another reason, the runtime reworded it or denied for a non-policy reason. The check accepts only the policy reasons `binary '/usr/bin/curl' not allowed` (the HTTP CONNECT proxy of `v0.0.116-rhaiv.1`) and `transparent_tcp_policy_denied` (the transparent proxy of v0.1.x); identity or resource denials such as `transparent_tcp_identity_unavailable` do not count. The curl error text also differs by runtime (`CONNECT tunnel failed, response 403` versus `Failed to connect`), so only `curl: (N)` is asserted.
- **Where to look**: `curl_denied_by_policy` in `tests/e2e/e2e-openshell-sandbox.sh`, `backends/openshell/profiles/agentic-ci-openai.yaml` and `agentic-ci-google-vertex-ai.yaml`, `openshell logs ci` in the sandbox

### Codex on OpenShell fails with "We're currently experiencing high demand" and websocket 500s
- **Likely cause**: The OpenShell proxy could not resolve the provider placeholder that Codex logged in with, and answered every request with HTTP 500 `credential_unavailable`, which Codex reports as high demand. `openshell logs ci` names the reason. The one reproduced so far is an `OPENAI_API_KEY` stored in the provider with a line break (`credential resolution rejected: resolved value contains prohibited characters`): OpenShell refuses to inject a secret containing CR, LF or NUL. A CI secret with a trailing newline is the likely source of the RHAI-2936 CI failures, but that was not confirmed against the real secret. agentic-ci strips surrounding whitespace before `provider create` and `provider update` and prints `Stripped surrounding whitespace (...) from OPENAI_API_KEY` when it does; a line break inside the key is rejected at setup. If the run log has no such line and the proxy log has no `credential_unavailable` denial, look elsewhere (for example a revoked key, which fails with 401, or a real OpenAI incident). The L4 endpoint is not the cause: the proxy replaces the placeholder on every request, including the websocket upgrade.
- **Where to look**: `_provider_credential()` and `_provider_process_env()` in `backends/openshell/provider.py`, `openshell logs ci` in the sandbox, the `codex_api::endpoint::responses_websocket` errors in the run log

### Claude Code ignores CLAUDE_CODE_OAUTH_TOKEN and uses Vertex AI or the API key
- **Likely cause**: `ANTHROPIC_API_KEY` is also set (it wins over the token), the token is empty, or the harness is not Claude Code. Only `ClaudeCodeHarness` selects the `oauth` auth mode; OpenCode and Codex ignore the token. The startup log shows `Auth: Claude subscription (OAuth token)` when the mode is selected. On the local backend the agent inherits the host environment, so a `CLAUDE_CODE_USE_VERTEX=1` left in the shell makes Claude Code use Vertex AI even though the log says OAuth token. A standalone runner image does the same when `GCP_SERVICE_ACCOUNT_KEY` is also set: its entrypoint exports `CLAUDE_CODE_USE_VERTEX=1`.
- **Where to look**: `ClaudeCodeHarness.auth_mode_for_env()` in `harness.py`, the `Auth:` line in the run log, `env | grep CLAUDE_CODE_USE` on the host for local runs, `_detect_tool` in `images/runner/shared/entrypoint.sh` for standalone images

### OpenShell setup fails with "no identifiable provider" or "Could not determine the existing OpenShell sandbox identity" after using a subscription token
- **Likely cause**: `oauth` mode creates the sandbox without a provider and records the mode only in the sandbox identity file (`~/.config/agentic-ci/openshell-sandbox.json`, or `AGENTIC_CI_OPENSHELL_STATE`). The file is cleared before each sandbox create and saved only when setup succeeds, so a failed `setup` or `run --keep` leaves a sandbox that cannot be identified. A file written by another mode, or edited by hand, gives the "no identifiable provider" error.
- **Where to look**: `OpenShellBackend.setup()`, `provider.requires_provider()`, the sandbox identity file; run `agentic-ci stop --backend openshell` to reset

### Codex exits before local or Podman execution with "credentials not found"
- **Likely cause**: None of `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, or `OPENAI_API_KEY` is available. Local runs may instead use `$CODEX_HOME/auth.json`; Podman runs require a forwarded environment credential.
- **Where to look**: `CodexHarness.validate_credentials()`, backend `setup()`, CI secret injection, and `CODEX_HOME`

## Jira client

### Markdown formatting lost in ADF roundtrip
- **Likely cause**: `adf_to_text()` was stripping markdown formatting. Fixed to preserve it during conversion.
- **Where to look**: `jira.py:adf_to_text()`

### `JiraClient.search()` results have empty summary, description, labels or comments
- **Likely cause**: The caller passed `fields=` without those Jira fields. `search()` keeps the same normalised keys but fills fields that were not requested (or that Jira omitted or returned as null) with empty defaults. Callers that only need keys should use `search_keys()`, which asks for `key` alone and pages up to 5000 issues per request with `nextPageToken`.
- **Where to look**: `jira/client.py:JiraClient.search()`, `search_keys()`, `_normalise_search_issue()`

## Gates

### Sensitive-files gate blocks files in directories named `secrets/`
- **Likely cause**: `check_sensitive_files` was matching patterns against both the filename and the full path. Python's `fnmatch` treats `*` as matching path separators, so the `*secret*` blocklist pattern matched directory components like `secrets/`. Fixed by restricting matching to the filename only (`os.path.basename`).
- **Where to look**: `gates.py:check_sensitive_files()`, fnmatch pattern matching

### Gate error or verdict failure says only "(SomeError); see the CI job log"
- **Symptom**: A tracker comment or `gate_errors` entry reads like `gitleaks pre-check failed: git rev-list error (CalledProcessError); see the CI job log` or `Verdict could not be loaded (ValueError); see the CI job log`, with no detail.
- **Likely cause**: By design (RHAI-2736). Gate error strings and the `gate_errors` that `run_skill()` passes to `label_applier` after a verdict load failure name only the exception class, because consumers post them to Jira and exception or stderr text can carry secrets. The full exception message and subprocess stderr are logged at ERROR level by `agentic_ci.gates` or `agentic_ci.skill` in the same job. `loader returned no verdict` means the retried `verdict_loader` returned `None` instead of raising.
- **Where to look**: the job log line `git rev-list failed: ...; stderr: ...`, `Could not ...: ...` or `[KEY] Failed to load verdict: <class>: <message>`; `gates.py:gitleaks_scan()` and the CLI gate runners, `skill.py:_verdict_error_text()`

### Comments or label authors with no email dropped
- **Symptom**: A comment from a Jira app or deleted user is missing from the agent context, or a label added by one fails the label-author check.
- **Likely cause**: By design. `filter_comments_by_domain()`, `check_label_author_email()` and `check_external_reporter()` treat a missing (`None`) or non-string email as outside the domain. Before RHAI-2736 a `None` `author_email` raised `TypeError` and aborted the run. `filter_bot_comments()` keeps a comment whose `body` is missing, since it holds no sentinel.
- **Where to look**: `gates.py:_email_matches()`, the `author_email` / `email` / `reporter_email` values from `jira/client.py`

## Container images

### AGENTS.md not found in container
- **Likely cause**: COPY paths in Containerfiles didn't match the repo layout after restructuring.
- **Where to look**: `images/runner/shared/Containerfile.base`, COPY directives

### OpenShell egress phase switch fails, or a profile preset is not reachable
- **Symptom**: `Could not switch to the <phase> egress phase: openshell policy get|set exited with status N; see the job log`, or a host from a sandbox profile's `egress` is denied.
- **Likely cause**: Phase switches replace the whole policy with `openshell policy set`; the `openshell stderr:` line before the error has the server's reason (a missing `filesystem_policy` or a `_provider_` rule name in the input are refused). Setup and validate egress is bound only to `/usr/local/bin/agentic-ci-sandbox-setup`: a command not started through the shim, a process it left behind, or an image without the shim is refused: a CONNECT `403` on OpenShell v0.0.116 (`Tunnel connection failed: 403`, curl `CONNECT tunnel failed, response 403`), a failed connect on v0.1.x (`[Errno 13] Permission denied`, curl `(7) Failed to connect`). Either way `openshell logs ci --source sandbox` has `DENIED <executable>(N) -> <host>:<port>`; a refusal without that line is not a policy denial. The shim is an extra layer, not isolation (any sandbox process could run it while shim rules are live); the protections are that shim rules exist only in setup/validate, agent rules are parked then, leftover processes are killed before every switch, and the credential provider is detached during setup/validate (see the next entry). While setup or validate egress is open, the agent's rules are parked (binary paths prefixed with `/proc/agentic-ci-parked`), so agent binaries are refused too until the switch back to `agent`; `setup()` switches a reused profile sandbox back to `agent`. Raw endpoints overlapping an agent-only host (`policy._AGENT_ONLY_HOSTS`: every `AUTH_ENDPOINTS` host, every endpoint host of the vendored provider profiles in `backends/openshell/profiles/` (the Vertex AI hosts come only from there), `oauth2.googleapis.com`, `*.aiplatform.googleapis.com` and `host.openshell.internal`, wildcards on either side compared as patterns) or carrying credential options are never opened to the shim (`WARNING: N egress endpoint(s) not opened in the <phase> phase`). Agent-phase presets are applied only at sandbox create; the profile hash in the identity file recreates the sandbox when the profile changes. With a profile, `.agentic-ci/openshell-policy.yml` is ignored by design (`Policy source: sandbox profile (repo policy file ignored)`).
- **Where to look**: `backends/openshell/policy.py` (`EGRESS_PRESETS`, `resolve_endpoints`, `phase_endpoints`), `backends/openshell/sandbox.py` (`build_phase_policy`, `apply_phase_policy`), `OpenShellBackend._set_egress_phase()`, `tests/e2e/e2e-openshell-profile.sh`

### OpenShell phase switch fails on leftover processes or the credential provider
- **Symptom**: `Could not stop the processes left in the sandbox before the <phase> phase: ...` (`N process(es) survived`, `the recorded main process is not running, so the record is stale`, or a scan exit status), `Could not identify the sandbox's main process: ...` (`no process was found`, `more than one candidate process was found`, `the only candidate is not the sandbox's sleep infinity`; the `sandbox processes: CMDLINE ...` line shows what it was, and both `sleep infinity` and the coreutils shebang form `/usr/bin/coreutils --coreutils-prog-shebang=sleep /usr/sbin/sleep infinity` are accepted), `The OpenShell sandbox's main process was not recorded; run agentic-ci stop ...`, `Could not detach|attach the credential provider for the <phase> phase: ...` (an exit status or `timed out`), or `Could not confirm that credential injection stops|resumes for the <phase> phase within 30s` (older releases say `API key provider` and `API key injection`). On a reused sandbox: `WARNING: could not prepare the existing sandbox: ...` followed by `Sandbox could not be reused; recreating OpenShell sandbox`. Or a setup or validate step, or the agent after one, gets 401s or `credential_unavailable` from the LLM API.
- **Likely cause**: With a sandbox profile, every egress phase switch first runs an in-sandbox scan (`openshell sandbox exec --no-login-shell -- /usr/bin/python3 -I -S -c <process scan>`, as the sandbox user) that SIGKILLs every sandbox-user process except the main `sleep infinity` (PID and start time saved as `main_process` in the identity file at create, where it must be the only sandbox-user child of the supervisor) and fails closed if one survives 10 s, if a `/proc` entry is unreadable, if the scan exits non-zero (a leftover can kill or stop it: same user), or on a 60 s exec timeout. `sandbox processes: KILLED pid=N comm='...'` lines in the job log name what was killed; error messages carry only counts. `MAIN_GONE` (scan exit 7) means the recorded main process is not running: OpenShell's supervisor exits with its entrypoint, so on a live sandbox the record is stale (a restart, or a wrong pick at create), and the scan kills nothing rather than take the sandbox down; `setup()` recreates a reused sandbox whose switch to `agent` fails for any reason, and `agentic-ci stop` does it by hand. A `sandbox supervisor is too old to honor --no-login-shell` line in the job log means the sandbox predates the gateway; recreate it. For openai, api-key and vertex auth, entering setup/validate probes a fresh exec for an `openshell:resolve:env:` placeholder in `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN` or `GOOGLE_VERTEX_AI_TOKEN` (attaching `ci-gcp` and waiting for one when it is missing, e.g. setup straight to validate), runs `openshell sandbox provider detach --wait --timeout 30 ci ci-gcp`, then probes until the placeholder is gone; entering agent attaches (also with `--wait`) and waits for it again. The supervisor polls the gateway every 10 s (`OPENSHELL_POLICY_POLL_INTERVAL_SECS`), so a slow or disconnected supervisor times out the 30 s wait. Detach and attach fail with `sandbox was modified by another operation` when something else changes the sandbox at the same time, and are cut off after 45 s when the gateway or supervisor does not answer (`--timeout 30` bounds the wait itself). LLM calls from setup or validate failing is by design (the credential is detached then); an agent failing auth right after a switch means the attach was not confirmed. The placeholder is the same before a detach and after the attach (checked on v0.1.2 by the profile e2e), so Codex's stored `auth.json` keeps resolving; if a later runtime issues a new one, the agent gets `credential_unavailable` until the next run logs in again. oauth has no provider. The shim is not a boundary: these steps, not the shim, keep agent processes and injection out of setup and validate; the real Anthropic key the env script exports for api-key auth is not covered.
- **Where to look**: `backends/openshell/sandbox.py` (`_PROCESS_SCRIPT`, `find_main_process`, `stop_leftover_processes`, `provider_env_state`, `detach_provider`, `attach_provider`, `wait_for_provider_env`), `backends/openshell/provider.py` (`provider_env_vars`), `OpenShellBackend._set_egress_phase()`, `_reuse_sandbox()` and `_sandbox_main_process()`, the identity file (`AGENTIC_CI_OPENSHELL_STATE`), `tests/e2e/e2e-openshell-profile.sh` sections 7 to 11

### Importing the OpenShell backend fails with `provider profile <file>.yaml ...`
- **Symptom**: `ValueError: provider profile <file>.yaml is not valid YAML` (or `is not a mapping`, `must have its file name as its id`, `endpoints must be a list`, `endpoint N has no host`) when anything imports `agentic_ci.backends.openshell`.
- **Likely cause**: `policy.py` reads every vendored provider profile at import to build the hosts the setup shim may never reach (`_AGENT_ONLY_HOSTS`), and fails loudly rather than open an inference host to the shim. A new or edited file in `profiles/` is malformed, or its `id` differs from its file name (which `provider.ensure_profile()` relies on).
- **Where to look**: `backends/openshell/provider.py` (`profile_endpoint_hosts`), `backends/openshell/profiles/*.yaml`, `backends/openshell/policy.py` (`_agent_only_hosts`)

### `e2e-openshell-profile.sh` denial check fails with `Sandbox log: no matching DENIED line`
- **Symptom**: A `DENIED`, `L7DENIED` or `not-injected` check fails although the probe was refused (`PROBE BLOCKED proxy-403|eacces`, `PROBE L7DENIED`, or curl's 403/`(7)` output), followed by `Sandbox log: no matching DENIED line for <host> after <mark>` (`no DENIED line for curl -> <host> after <mark>` in the key checks).
- **Likely cause**: A refusal counts only with the sandbox log's `DENIED` line for that caller (python3 or curl) and host, stamped strictly after a mark taken right before the probe (the later of the host's clock and the newest such line already in the log, so an earlier check's line never counts), read with `openshell logs ci --source sandbox --since 10m -n 5000` for up to 20 s. No line means the refusal did not come from the policy (DNS, network, a sandbox-side error), the supervisor did not ship the log in time, the host clock runs ahead of the sandbox clock (they share a kernel in the nested-podman setup, so they should not drift), or the log format changed (`NET:OPEN ... DENIED /usr/bin/curl(0) -> host:443`; L7: `DENIED PUT http://host:443/...`).
- **Where to look**: `tests/e2e/e2e-openshell-profile.sh` (`denial_mark`, `denial_logged`, the `denials.py` filter, `expect`, `key_check`)

### A request to an egress preset host gets HTTP 403 `policy_denied`, or a TLS error
- **Symptom**: A tool reaching a preset host (PyPI, npm, the Go proxy, GitHub release assets) gets an HTTP `403` whose JSON body has `"error": "policy_denied"`, or fails certificate verification, while plain downloads work.
- **Likely cause**: Preset endpoints are `host:443:read-only:rest:enforce`, so the OpenShell proxy terminates TLS and allows only `GET`, `HEAD` and `OPTIONS`; a `PUT`/`POST` (such as `npm publish` or a bucket upload) is denied by design. A certificate error means the tool ignores the proxy CA that OpenShell exposes through `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and `NODE_EXTRA_CA_CERTS`. Every other endpoint on port 443 for a preset host (the default L4 `pypi.org` and `files.pythonhosted.org`, or a raw or `--policy` entry, wildcards included) is replaced by the preset's L7 one in every phase, so a raw `full` endpoint for that host does not reopen writes; a replaced raw or `--policy` endpoint logs `WARNING: N egress endpoint(s) for an egress preset host replaced`.
- **Where to look**: `backends/openshell/policy.py` (`EGRESS_PRESETS`, `_merge_endpoints`), `tests/e2e/e2e-openshell-profile.sh` (section 6)

### Toolchain `failed` with a checksum mismatch or "no sha256"
- **Symptom**: The log has `Toolchain <name>: <requested> -> <version>: failed (<name> <version>: checksum mismatch; the download was discarded)`, `... the official checksum file has no sha256 for it`, `... the registry lists no sha512 integrity` or `protoc <version> publishes no checksum and is not in agentic-ci's vendored table`, and `_run/toolchains.json` records `status: failed`. The run continues without that toolchain.
- **Likely cause**: Fail-closed by design (RHAI-2619). The downloaded archive did not match the digest the tool's official checksum source publishes (a corrupted or tampered download, a proxy rewriting the body), the release has no entry for this file or machine, or a protoc version before 36.0 is not in `PROTOC_SHA256`. Nothing unverified is cached or installed; a cached archive that no longer verifies is deleted (`WARNING: cached <name> archive does not verify; downloading again`). `a download left the tool's official hosts; refused` means the URL or a redirect pointed outside the entry's `hosts`, or, for a GitHub release, a github.com hop left the entry's `/<owner>/<repo>/releases/` path (a renamed or transferred repo) or a GitHub asset host was not the last hop (the reason is on the `toolchain download refused:` line).
- **Where to look**: `toolchains.py` `CATALOG` (`url`, `checksum_url`, `hosts`), the `_resolve_*` functions, `fetch_archive()`, `PROTOC_SHA256`; the `toolchain fetch:` lines in the job log; the host cache (`~/.cache/agentic-ci/toolchains`, or `AGENTIC_CI_TOOLCHAIN_CACHE`)

### Toolchain `auto` does not resolve
- **Symptom**: `Toolchain go: auto -> -: failed (go: auto needs a go.mod at the workdir root)`, `node: auto found no .nvmrc, .node-version, package.json engines.node or .tool-versions nodejs line`, `node: package.json engines.node is not a simple version ...`, `pnpm: package.json packageManager is not pnpm@X.Y.Z[+sha512.<hex>]`, `... auto read a version from <file> that is not digits`, `<file> is a symlink that leads out of the workdir`, `<file> is larger than N bytes`, `<name>: auto is not supported; pin a version`, `python <v> is neither in the newest python-build-standalone release nor in agentic-ci's table of earlier builds`, `python <v> matches no build in the newest python-build-standalone release`, or `protoc: <v> has too many parts; use an exact MAJOR.MINOR version`.
- **Likely cause**: `auto` reads only the files listed in docs/sandbox-profiles.md (`auto` sources), at the workdir root, and accepts only digit versions and the simple `engines.node` forms (exact, major, `^`/`~`/`>=` a major). Ranges, `lts/*`, `go 1.27rc1`, a `packageManager` for another manager or a hash other than sha512 are errors by design; messages name the file, never its content. A partial version for a tool without a public index (shfmt, golangci-lint, helm, yq, kustomize, buf, protoc) is an error too, and so is a 3-part protoc version. A partial python version resolves only against the newest python-build-standalone release, which has one patch per minor; an exact one it lacks needs an entry in `PYTHON_BUILD_TAGS`. go takes the higher of the go.mod `toolchain` and `go` lines (`toolchain default` counts as absent). Pin the version in the central profile when the repo's files cannot express it.
- **Where to look**: `toolchains.py` `_auto_go()` / `_auto_node()` / `_auto_pnpm()` / `_auto_python()`, `_read_repo_file()`, `_pick()`, `CatalogEntry.partial`

### Toolchain extraction fails in the sandbox
- **Symptom**: `failed (<name> <version>: extraction in the sandbox failed; see the job log)` with a `toolchain install: FAILED <ExceptionClass>` line, `... an archive member points outside the target directory; nothing was installed` (or `an archive link points outside ...`, `the archive expands beyond the size limit`, ...), or `the provisioning check failed`.
- **Likely cause**: Extraction runs with the sandbox image's `/usr/bin/python3 -I -S` (`tarfile`, `zipfile`, `lzma`), never on the host. An image without python3, or with a python3 missing those modules, fails every toolchain (the Hummingbird sandbox images ship python3, tar, gzip and xz but no unzip, and nothing uses unzip). A `REFUSED <class>` line is the archive check working as designed: a member with an absolute path or `..`, a link resolving outside the target (through the archive's own symlinks too), a member under a symlink, a duplicate, a special file, more than 200000 members or over 4 GiB. `REFUSED checksum` means the uploaded file no longer matches the sha256 the host verified; `REFUSED upload` that the uploaded name is a symlink or not a regular file (something in the sandbox replaced it). `upload to the sandbox timed out` is the 900 s upload limit. A full `/sandbox` (disk) shows up as `FAILED OSError`.
- **Where to look**: `backends/openshell/provision.py` `INSTALL_SCRIPT`, `REFUSALS`, `OpenShellInstaller`; `sandbox.exec_python()`; the image's python3 (`images/runner/*/Containerfile.openshell`)

### `go` refuses a go.mod: `go.mod requires go >= X (running go Y; GOTOOLCHAIN=local)`
- **Likely cause**: By design. The env script exports `GOTOOLCHAIN=local` with a provisioned go, so Go never downloads another toolchain (the sandbox cannot reach `dl.google.com` or the module proxy's `golang.org/toolchain` anyway without the goproxy preset). The provisioned go is older than what the go.mod asks for: a pinned `go:` version in the profile, a `go` line newer than the `toolchain` line `auto` read, or a nested module with its own go.mod. Use `go: auto` or pin a version that satisfies every module the agent builds.
- **Where to look**: `_run/toolchains.json` (`resolved` for go), `toolchains.py` `toolchain_env()` / `_auto_go()`, the repo's go.mod files

### Sandbox profile setup step `timeout` or `failed`
- **Symptom**: `Setup step <name>: timeout (exit 124, Ns)` (the tail ends with `agentic-ci: step timed out after Ns; stopping it`) or `Setup step <name>: failed (exit N, ...)` and `WARNING: K setup step(s) did not pass; the run continues`; `_run/sandbox-setup.json` has the step with that `status`, and `ENVIRONMENT.md` tells the agent.
- **Likely cause**: By design a failed or timed-out step is recorded and the next one still runs; the agent is told. A timeout is the step's own `timeout` (default 600 s, at most 3600), enforced inside the sandbox by the step wrapper (SIGTERM to the shim and its tree, SIGKILL 10 s later). `status: timeout` with `rc: null` means the host gave up on the exec itself: a gateway problem, or a step wrapper that could not reap what the step left (the wrapper becomes a child subreaper and SIGKILLs every process below it once the shim exits, so a daemon holding the step's output no longer keeps the exec open; if `ctypes` or `prctl` is unavailable in the image's python3, orphans escape to init and can hold it). A step that needs the network fails when its host is not in a preset open in the setup phase (check `Egress phase setup: N setup-shim endpoint(s) open` and the sandbox log's `DENIED ... [policy:...]` lines). Exit 127 (`command not found`) means the command is not on the step's `PATH` (toolchain directories, then `/sandbox/.local/bin` and the system directories; the image's own `PATH` extras such as `/sandbox/.venv/bin` are not there). Steps get no credential and no variable other than those in docs/sandbox-profiles.md (How a step runs). Every step is `not_run` when the setup phase could not be opened (see the phase switch entries above).
- **Where to look**: `_run/sandbox-setup.json` (the tail is redacted and capped at 50 lines), `backends/openshell/steps.py` (`STEP_WRAPPER`, `run_step()`, `build_env()`), `OpenShellBackend._run_setup_phase()`

### A reused sandbox did not run its setup steps again
- **Likely cause**: By design. Setup steps run when the sandbox is created; a reused sandbox (same identity, so the same profile hash) keeps their effects, and `setup()` reports the results saved in the sandbox identity (`setup` key of `~/.config/agentic-ci/openshell-sandbox.json`). Run `agentic-ci stop` to recreate the sandbox and run them again.
- **Where to look**: `backends/openshell/__init__.py` `_saved_setup_records()`, `_SETUP_KEY`

### Validate commands all `not_run`, or `could not delete the harness credential files before validation`
- **Symptom**: `WARNING: the validate phase could not be opened; validation not run` and every validate record `status: not_run`, often after `validate phase error: Could not delete the harness credential files before validation: ...`.
- **Likely cause**: The validate phase kills every leftover process and then deletes the harness credential files (`/sandbox/.codex/auth.json`, `/sandbox/.claude/.credentials.json`, `/sandbox/.local/share/opencode/auth.json`, `/tmp/.agentic-ci-env.sh`) before it detaches the provider. The wipe must be confirmed (`credential wipe: WIPED left=0`); a `LEFT <index>` line means a path could not be removed (a directory the agent made read-only, for example), and validation is skipped rather than run next to a credential. A kill or provider failure has the same effect (see the phase switch entries above). The run's exit code is not affected. `auth.json` being gone afterwards is expected: Codex logs in again at the next agent start.
- **Where to look**: job log `credential wipe:` lines, `steps.wipe_files()`, `OpenShellBackend._run_validation()` / `_credential_files()`, `Harness.sandbox_credential_files`

### A validate command fails only in the harness re-run
- **Likely cause**: The re-run gets the built step environment (toolchains, profile `env`, CA variables) and runs with `--no-login-shell` after every agent process was killed, so it does not see what the agent's shell had: variables the agent exported, a virtualenv activated in `~/.bash_profile`, a service the agent started, or the credential files (deleted before validate). Make the validate command self-contained, or add what it needs to the profile's `env` or `setup`.
- **Where to look**: `_run/sandbox-validation.json` tails, docs/sandbox-profiles.md (Validate commands)

### `_run/sandbox-*.json` differs from what the agent wrote there, or is missing
- **Likely cause**: By design. The host writes `sandbox-setup.json`, `sandbox-validation.json`, `sandbox-discard.json` and `toolchains.json` again after the workdir download, so a record the agent forged (or a symlink or directory it left at that path) is replaced by the host's, and every one of those names this run did not write is removed (`Removed <file> from the run directory: this run did not write it`), with or without a profile. `sandbox-validation.json` is written only by a run that validated, so it is absent after a classifier run. `WARNING: the run directory was not a directory after the download` means the agent replaced `_run` with a symlink or file; it is replaced by an empty directory (the link's target is never touched). `WARNING: <file> could not be written` means the host write failed (the log names the exception class).
- **Where to look**: `OpenShellBackend._write_run_records()` / `_reclaim_run_dir()` / `_write_run_record()` / `_remove_run_record()`

### A `discard_before_download` path is `refused` or still downloaded, or missing in a later run
- **Likely cause**: The path is moved (not deleted) to `/sandbox/.agentic-ci/discarded` in the sandbox, never following a symlink: a path with a symlink on the way (`sub` -> elsewhere for `sub/x`) is `refused`, and one whose last component is a symlink has only the link removed. `absent` means nothing was there; `error` that the in-sandbox move did not report back (timeout, missing workdir). Paths with `..`, absolute paths and `.git` are rejected when the profile is parsed. The next `run()` on the same sandbox moves the paths back before the agent starts (`Restored N path(s) discarded by the previous run`); a path recreated in the meantime is kept and the stash dropped, and `WARNING: the paths discarded by the previous run could not be restored` means the restore failed (the agent then lacks, for example, `node_modules` the setup steps installed).
- **Where to look**: `_run/sandbox-discard.json`, `steps.DISCARD_SCRIPT` / `RESTORE_SCRIPT` / `discard_paths()` / `restore_discarded()`

### `The sandbox is not in the agent phase; switching it before the agent`, then a phase switch error from `run()`
- **Likely cause**: An earlier switch back to the `agent` phase failed on this backend (after validation, or after setup), so the sandbox may still have validate or setup egress open, the provider detached or leftovers running. `run()` switches to `agent` before it writes the env script (which can hold a real key in api-key or oauth mode) and raises while that fails; no agent starts. `setup()` does the same for a reused sandbox and recreates it when the switch fails. See the phase switch entries above for the underlying error.
- **Where to look**: `OpenShellBackend._agent_phase_pending`, `_set_egress_phase()`, `run()`

### `WARNING: N profile env variable(s) not exported to the agent: agentic-ci sets them`
- **Likely cause**: By design. The profile's `env` is exported last in the env script, and a name the script already exports (the harness's own variables, `TRACEPARENT`, `AGENT_MODEL`, ...) or that the caller passes in `extra_env` is dropped so agentic-ci's value wins; the `profile env not exported:` line names them. Names on the reserved list (docs/sandbox-profiles.md, Reserved environment variable names, now including `OPENCODE_`, `AGENTIC_CI_`, `OPENSHELL_` and `XDG_` prefixes) are rejected when the profile is parsed instead.
- **Where to look**: `OpenShellBackend._profile_script_lines()`, `sandbox_profile._ENV_DENY_PREFIXES` / `_ENV_DENY_NAMES`

### Validate commands did not run after a routed skill's classifier
- **Likely cause**: By design. The classifier run of `run_routed_skill` changes no code, so it passes `validate=False` and only the skill run that follows validates (`_run/sandbox-validation.json` is from the skill run; the classifier run writes none and removes any left there). Without a validate phase the leftover processes are still killed before the discard (`Stopped N leftover sandbox process(es) before the discard phase`).
- **Where to look**: `skill.py` `run_routed_skill()` / `_AgentSession.run()`, `Backend.validate_after_run`

### `ENVIRONMENT.md` missing in the sandbox
- **Symptom**: `WARNING: ENVIRONMENT.md could not be written; the run continues` with an `environment files` line.
- **Likely cause**: Best effort: the upload or the in-sandbox move failed. Written only with a sandbox profile, to `/sandbox/.agentic-ci/` (outside the workdir, never downloaded). A symlink or file the agent left at that path is replaced.
- **Where to look**: `OpenShellBackend._write_environment()`, `backends/openshell/environment.py`, `steps.ENVIRONMENT_SCRIPT`

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

### OpenShell rejects a policy: "would give provider 'ci-gcp' both profile endpoint bindings and sandbox policy credential bindings"
- **Likely cause**: A sandbox policy binds the provider (`credential_binding.provider: ci-gcp`) while its profile declares endpoints; OpenShell v0.1.x refuses the combination. agentic-ci stopped writing credential bindings with the move to v0.1.2 (the endpointless `google-cloud` provider needed them; `agentic-ci-google-vertex-ai` declares its hosts). A binding can still arrive from a repo `.agentic-ci/openshell-policy.yml` or a hand-edited policy; phase switches carry any existing binding through unchanged.
- **Where to look**: `backends/openshell/sandbox.py:_apply_policy()`, `openshell policy get ci --base -o json`

### `openshell policy update` rejects the sandbox policy: "network endpoint ambiguity validation failed ... conflicting metadata"
- **Likely cause**: The sandbox policy declares a host the attached provider's profile also declares, with different metadata (protocol, access, enforcement, TLS or the uninspected-credentials opt-in). The Vertex profile declares the aiplatform hosts as L7 `rest`, so `policy.AUTH_ENDPOINTS["vertex"]` is empty; a repo `.agentic-ci/openshell-policy.yml` that adds `aiplatform.googleapis.com` or `*-aiplatform.googleapis.com` trips it. The `api.openai.com` and `api.anthropic.com` entries in `AUTH_ENDPOINTS` match their L4 profiles exactly (`allow-uninspected-credentials`), which OpenShell accepts.
- **Where to look**: `backends/openshell/policy.py:AUTH_ENDPOINTS`, `backends/openshell/profiles/*.yaml`, the repo policy file, `openshell policy get ci --full -o json`

### Gateway never becomes healthy: gateway.toml rejected (`unknown field `compute_drivers`` or `unsupported gateway config version 1`)
- **Symptom**: `agentic-ci run --backend openshell` fails with `RuntimeError: Gateway did not become healthy within 60s`; the gateway log shows `TOML parse error ... unknown field `compute_drivers`` or `unsupported gateway config version 1; this build requires version 2`.
- **Likely cause**: Since `v0.0.116-rhaiv.8` (v0.1.x included) gateway.toml is schema version 2: `[openshell] version = 2`, a singular `compute_driver`, driver options under `[openshell.drivers.<name>]`. agentic-ci renders that; an older agentic-ci against a newer gateway (or the reverse) fails this way. A running gateway started with an older file is restarted by `setup()` when the rendered file changes (`OpenShell gateway config changed; restarting gateway`).
- **Where to look**: `backends/openshell/gateway.py` (`_GATEWAY_TOML`, `config_is_current`), `~/.local/state/openshell/gateway-*.log`

### Provider creation fails: `provider profile '<id>' not found; import a matching profile before using this provider type`
- **Likely cause**: The gateway ships no provider profiles (since `v0.0.116-rhaiv.8`) and its database is fresh on every start. agentic-ci imports its own (`provider.ensure_profile()`) before `provider create`; a type agentic-ci does not vendor (`google-cloud`, `google-vertex-ai`, `openai`, `anthropic`) is not found. Check that `profiles/<id>.yaml` exists and that `openshell provider profile export <id> -o yaml` shows it after setup.
- **Where to look**: `backends/openshell/provider.py` (`ensure_profile`, `*_PROFILE_ID`), `backends/openshell/profiles/`

### Vertex runs on OpenShell fail after reusing a gateway: `Could not load the default credentials`, requests to `127.0.0.1:8174`, or a `google-cloud` provider in `openshell provider list`
- **Likely cause**: A `ci-gcp` provider of type `google-cloud` (from agentic-ci before the v0.1.2 move) relies on the GCE metadata emulator, which OpenShell removed; v0.1.x still injects `GCE_METADATA_HOST`/`GCE_METADATA_IP=127.0.0.1:8174` and `METADATA_SERVER_DETECTION=assume-present` for it, so SDKs try a port nothing serves (`curl: (7) Failed to connect to 127.0.0.1:8174`). `provider.auth_mode()` maps `google-cloud` and `google-vertex-ai` to non-Vertex modes, so `setup()` deletes that provider and the sandbox and recreates them from `agentic-ci-google-vertex-ai` (`Auth mode changed; recreating OpenShell sandbox and provider`). If the variables still show up in a fresh exec (`openshell sandbox exec --name ci -- env | grep GCE_METADATA`), the provider was not replaced.
- **Where to look**: `backends/openshell/provider.py` (`_PROVIDER_AUTH_MODES`), `openshell provider list -o json`

### `openshell sandbox create` times out: `ConfigurationInvalid: Effective configuration could not be activated`
- **Symptom**: `sandbox provisioning timed out`; the supervisor log (`/var/log/openshell.<date>.log` inside the `openshell-supervisor-<id>` container) shows `Image policy is invalid; replace the sandbox policy to repair configuration`.
- **Likely cause**: The Hummingbird agentic images ship `/etc/openshell/policy.yaml` in a foreign schema, and OpenShell (since `v0.0.116-rhaiv.8`) rejects it instead of falling back. agentic-ci always passes `--policy` with `policy.BASE_POLICY`; the policy schema also refuses null and unknown fields, so a hand-written base policy with `key: null` fails the same way.
- **Where to look**: `backends/openshell/sandbox.py:create()`, `backends/openshell/policy.py:BASE_POLICY`

### Sandbox creation pulls `ghcr.io/nvidia/openshell/sandbox`, or every host fails to resolve in the sandbox
- **Likely cause**: The podman driver needs both `supervisor_image` and `sandbox_runtime_image` in gateway.toml; without `OPENSHELL_SANDBOX_RUNTIME_IMAGE` it falls back to the upstream ghcr.io image (unreachable from CI). The workload runs with `network=none` and resolves through the policy DNS relay on `127.0.0.53`; since v0.1.0 the driver writes that `/etc/resolv.conf` itself (`v0.0.116-rhaiv.8` to `rhaiv.11` did not, and agentic-ci baked one into the images then). An empty resolv.conf in the sandbox means an older runtime.
- **Where to look**: `images/ci/Containerfile.openshell` ENV lines, `backends/openshell/gateway.py` (`_DRIVER_IMAGES`), `openshell sandbox exec --name ci -- cat /etc/resolv.conf`

### Claude Code or OpenCode on Vertex fails inside OpenShell: `Could not refresh access token`, `Could not load the default credentials`, or `Was there a typo in the url or port?`
- **Likely cause**: There is no metadata server in the sandbox; the provider injects `GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN` (service account) or `GOOGLE_VERTEX_AI_TOKEN` (gcloud ADC) as a placeholder. Claude Code's env script sets `CLAUDE_CODE_SKIP_VERTEX_AUTH=1`, `ANTHROPIC_AUTH_TOKEN=<placeholder>` and `ANTHROPIC_VERTEX_BASE_URL`; OpenCode's writes an `external_account` ADC whose token URL is `agentic-ci vertex-token-stub` on `127.0.0.1:8175` (log in `/tmp/.agentic-ci-vertex-stub.log`). "typo in the url or port" means the stub is not listening: an image whose agentic-ci predates the subcommand, or an empty placeholder (provider detached, for example a run that died in a shim phase). A `401` from Google with `ALLOWED ... [policy:_provider_ci_gcp` in the sandbox log means the token itself is bad (rotation failed: see `[token-keepalive] rotate failed`).
- **Where to look**: `harness.py` (`_OPENSHELL_VERTEX_ADC_STUB_LINES`, `ClaudeCodeHarness.build_env_script_lines`), `vertex_token_stub.py`, `backends/openshell/profiles/agentic-ci-google-vertex-ai.yaml`

### Routed Codex run falls back: `classifier exited with code -15`
- **Symptom**: `run_routed_skill` on the OpenShell backend logs `stream completed (rc=-15) but verdict file .../verdict.json missing; keeping original exit code` right after the classifier wrote `_run/route.json`, then `Model routing fell back to <default>: classifier exited with code -15`.
- **Likely cause**: Codex does not exit within the 10s grace period after its final stream event, so the backend terminates it (`-15`). The backend only promotes a terminated-after-completion run to 0 when its completion file exists. The classifier run now passes `completion_file=route.json` to the session, so the check looks for the route file rather than the skill verdict. If this reappears, check that `_router` in `skill.py` still wraps `session.run` with `completion_file`.
- **Where to look**: `skill.py` `_AgentSession.run(completion_file=...)` and `_router`, `backend.py` `_resolve_exit_code()`, `routing.py` `classify()`

### Codex OTel data missing or `openshell sandbox download` returns a corrupt tar on OpenShell rhaiv.11
- **Symptom**: Codex runs finish but the OTLP artifact has no Codex response (autofix e2e: `Native Codex OTel response missing or invalid (model=missing, tokens=0)`), and the sandbox log (`openshell logs ci --source sandbox`, or `/var/log/openshell.<date>.log` in the supervisor container) has `DENIED host.openshell.internal:53 [reason:policy_dns_trusted_gateway_unavailable]` and no `ALLOWED /usr/local/bin/codex(...) -> host.openshell.internal:<port>` line. Or the workdir download fails with `failed to extract tar archive from sandbox ... numeric field did not have utf-8 text when getting cksum`.
- **Likely cause**: The OTLP failure is the `v0.0.116-rhaiv.8` to `rhaiv.11` podman driver: it passed no trusted host gateway (`host_gateway_ip: None`), so policy DNS refused every lookup of `host.openshell.internal` from the `network=none` workload and Codex's OTLP/HTTP export to the collector never left the sandbox. v0.1.x defaults the trusted gateway to `127.0.0.1` and maps the name (`Policy DNS mapped host.openshell.internal resolved=127.0.0.1 synthetic=198.18.0.2`); agentic-ci must leave `host_gateway_ip` unset. The corrupt tar was most likely the supervisor crashing under many concurrent relays (NVIDIA/OpenShell#3396, fixed by #3642, in v0.1.0 and later). agentic-ci 0.3.62 moved to rhaiv.11 and was reverted to rhaiv.1; the move to v0.1.2 re-lands it. `tests/e2e/e2e-openshell-profile.sh` section 10 checks that `/v1/logs` still reaches the collector after a stalled response of 150 s, past a policy DNS mapping's lifetime.
- **Where to look**: `images/ci/Containerfile.openshell` `OPENSHELL_IMAGE_TAG`, `backends/openshell/gateway.py` (no `host_gateway_ip`), `backends/openshell/sandbox.py` (the `host.openshell.internal:<port>` rule), the sandbox log

### Codex or OpenCode agent cannot find a skill script (`can't open file '/scripts/write_json.py'`)
- **Symptom**: The agent runs a skill command such as `uv run --script ${CLAUDE_SKILL_DIR}/scripts/write_json.py` and gets `can't open file '/scripts/write_json.py'`, then guesses the plugin cache path itself (`export CLAUDE_SKILL_DIR=/sandbox/.codex/plugins/cache/...`).
- **Likely cause**: `CLAUDE_SKILL_DIR` is unset. Claude Code fills it in; for Codex and OpenCode the backend exports what `agentic-ci skill-dir <skill>` finds, and exports nothing when the run has no skill name (a custom `container_runner`, or `agentic-ci run`), the image's agentic-ci predates `skill-dir` (exit 2), no enabled plugin ships the skill, or two do.
- **Where to look**: `agentic-ci skill-dir <skill>` inside the sandbox or container (with the run's `AGENT_ENABLED_PLUGINS` applied), `codex plugin list --json`, `plugins.py:find_skill_dir()`, `Harness.skill_dir_script_lines()`, `PodmanBackend._skill_dir_env_args()`, `LocalBackend._set_skill_dir()`

### OpenCode image build fails with `KeyError: 'repo'`
- **Likely cause**: A marketplace plugin uses a `git-subdir` source with `url` and `path` instead of the legacy GitHub `repo` field. The OpenCode compatibility installer must resolve both source formats and search for skills relative to the configured subdirectory.
- **Where to look**: `plugins.py:install_opencode_skills()`, the generated marketplace entry

### A plugin's skills are missing although the plugin is installed (`provide no skills`)
- **Symptom**: The image build log ends with `WARN: N plugin(s) provide no skills: <plugin>`, or a run that enables the plugin through `AGENT_ENABLED_PLUGINS` reports it. With Claude Code or native Codex plugins, `enable-plugins` prints `WARNING: enabled plugin(s) provide no skills: <plugin>` and the agent runs without those skills. With OpenCode or the Codex skills compatibility layer, `enable-plugins` exits 1 with `unknown plugin(s) in AGENT_ENABLED_PLUGINS` and the run aborts before the agent starts.
- **Likely cause**: The skills-registry entry points at a path that holds no skills. `odh-ai-helpers` pointed at the deleted `helpers/skills` for five weeks after ai-helpers split into `odh-*` plugins, and Claude Code loaded 0 of its skills. Other causes: the skills are symlinks (the deprecated `odh-ai-helpers` umbrella; Claude Code follows them, the OpenCode and Codex installs drop them), the upstream install failed, or under OpenCode a skill name collides with a plugin installed earlier (`destination path collision(s): ... (already installed by <plugin>)`). MCP-only plugins and bundles such as `pf-mcp` and `patternfly` legitimately provide no skills.
- **Where to look**: the plugin's entry in skills-registry, `/usr/local/share/agentic-ci/plugin-skills.manifest.json` in the image, `plugins.py` `_claude_skill_names()` / `_codex_skill_names()` / `install_opencode_skills()`. For Claude Code, `claude plugin details <plugin>@<marketplace>` shows the skills it actually loads.
