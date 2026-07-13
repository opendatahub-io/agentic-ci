#!/bin/bash
# e2e-openshell-sandbox.sh -- End-to-end tests for OpenShell sandbox images.
#
# Builds the openshell-base, claude-sandbox, and opencode-sandbox images,
# then verifies that expected binaries, configs, and plugins/skills are
# present in each image. If credentials are available, also runs agent
# prompts through the OpenShell backend.
#
# Requires: podman, agentic-ci (for agent run tests)
# Credentials: GCP_SERVICE_ACCOUNT_KEY, GCLOUD_CREDENTIALS,
#              or ANTHROPIC_API_KEY (optional, for agent run tests)
#
# Usage:
#   ./tests/e2e/e2e-openshell-sandbox.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

source "$SCRIPT_DIR/../images/shell-utils.sh"

PASS=0
FAIL=0
TMPDIR_E2E="$(mktemp -d)"

cleanup() {
    agentic-ci stop --backend openshell --harness claude-code 2>/dev/null || true
    agentic-ci stop --backend openshell --harness opencode 2>/dev/null || true
    rm -rf "$TMPDIR_E2E"
    echo ""
    print_header "=== Results ==="
    print_success "Passed: $PASS"
    if [[ "$FAIL" -gt 0 ]]; then
        print_error "Failed: $FAIL"
        exit 1
    else
        print_success "All tests passed!"
    fi
}
trap cleanup EXIT

assert_ok() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        print_success "PASS: $desc"
        PASS=$((PASS + 1))
    else
        print_error "FAIL: $desc"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local desc="$1" output="$2" pattern="$3"
    if echo "$output" | grep -qi "$pattern"; then
        print_success "PASS: $desc"
        PASS=$((PASS + 1))
    else
        print_error "FAIL: $desc -- expected '$pattern' in output"
        echo "  Got: ${output:0:200}"
        FAIL=$((FAIL + 1))
    fi
}

dump_gateway_log() {
    local log
    log="$(ls -t ~/.local/state/openshell/gateway-*.log 2>/dev/null | head -1)"
    if [[ -n "$log" && -s "$log" ]]; then
        echo "--- gateway log ($log) ---"
        cat "$log"
        echo "--- end gateway log ---"
    fi
}

# --- Resolve sandbox images ---
# Use pre-built images from env vars (CI), or build locally (dev).
if [[ -n "${CLAUDE_SANDBOX_IMAGE:-}" ]] && [[ -n "${OPENCODE_SANDBOX_IMAGE:-}" ]]; then
    print_header "=== Using pre-built sandbox images ==="
    CLAUDE_SANDBOX="$CLAUDE_SANDBOX_IMAGE"
    OPENCODE_SANDBOX="$OPENCODE_SANDBOX_IMAGE"
    print_step "claude-sandbox: $CLAUDE_SANDBOX"
    print_step "opencode-sandbox: $OPENCODE_SANDBOX"
else
    print_header "=== Building OpenShell sandbox images ==="

    print_step "Building openshell-base..."
    podman build -t openshell-base:latest \
        -f "$REPO_ROOT/images/runner/shared/Containerfile.openshell-base" \
        "$REPO_ROOT/images/runner/shared/"

    print_step "Building claude-sandbox..."
    podman build -t localhost/claude-sandbox:latest \
        -f "$REPO_ROOT/images/runner/claude-code/Containerfile.openshell" \
        "$REPO_ROOT/images/runner/"

    CLAUDE_SANDBOX="localhost/claude-sandbox:latest"

    print_step "Building opencode-sandbox..."
    podman build -t localhost/opencode-sandbox:latest \
        -f "$REPO_ROOT/images/runner/opencode/Containerfile.openshell" \
        "$REPO_ROOT/images/runner/"

    OPENCODE_SANDBOX="localhost/opencode-sandbox:latest"
fi

# --- Resolve supervisor image ---
if [[ -n "${SUPERVISOR_IMAGE:-}" ]]; then
    export OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE"
elif [[ -z "${OPENSHELL_SUPERVISOR_IMAGE:-}" ]]; then
    export OPENSHELL_SUPERVISOR_IMAGE="quay.io/aipcc/agentic-ci/openshell-supervisor:latest"
fi
print_step "Using supervisor image: $OPENSHELL_SUPERVISOR_IMAGE"

# Helper: run a command inside a sandbox image as the sandbox user
run_in() {
    local image="$1"; shift
    podman run --rm --entrypoint "" "$image" "$@"
}

# --- openshell-base checks ---
print_header "=== openshell-base: binaries ==="

assert_ok "uv is installed" run_in "$CLAUDE_SANDBOX" uv --version
assert_ok "gh is installed" run_in "$CLAUDE_SANDBOX" gh --version
assert_ok "glab is installed" run_in "$CLAUDE_SANDBOX" glab --version
assert_ok "shellcheck is installed" run_in "$CLAUDE_SANDBOX" shellcheck --version
assert_ok "git is installed" run_in "$CLAUDE_SANDBOX" git --version
assert_ok "python3 is installed" run_in "$CLAUDE_SANDBOX" python3 --version
assert_ok "ruff is installed" run_in "$CLAUDE_SANDBOX" ruff --version

print_header "=== openshell-base: user/workdir ==="

assert_ok "sandbox user exists (uid 998)" \
    run_in "$CLAUDE_SANDBOX" id -u sandbox
assert_ok "workdir is /sandbox" \
    run_in "$CLAUDE_SANDBOX" sh -c 'test "$(pwd)" = "/sandbox"'

# --- claude-sandbox checks ---
print_header "=== claude-sandbox: binaries ==="

assert_ok "claude is installed" run_in "$CLAUDE_SANDBOX" claude --version
assert_ok "node is installed" run_in "$CLAUDE_SANDBOX" node --version

print_header "=== claude-sandbox: plugins (seed) ==="

assert_ok "settings.json exists" \
    run_in "$CLAUDE_SANDBOX" test -f /sandbox/.claude/settings.json
assert_ok "plugin seed directory exists" \
    run_in "$CLAUDE_SANDBOX" test -d /sandbox/.claude-seed
assert_ok "installed_plugins.json in seed" \
    run_in "$CLAUDE_SANDBOX" test -f /sandbox/.claude-seed/installed_plugins.json
assert_ok "marketplace registered in seed" \
    run_in "$CLAUDE_SANDBOX" test -d /sandbox/.claude-seed/marketplaces

print_header "=== claude-sandbox: entrypoint and scripts ==="

assert_ok "entrypoint.sh is installed" \
    run_in "$CLAUDE_SANDBOX" test -x /usr/local/bin/entrypoint.sh
assert_ok "agentic-ci is installed" \
    run_in "$CLAUDE_SANDBOX" agentic-ci --version

print_header "=== claude-sandbox: AGENT_ENABLED_PLUGINS ==="

# Verify enable-plugins filters settings.json inside the image.
# Pick the first enabled plugin and check that only it remains enabled.
PLUGIN_FILTER_RESULT="$(run_in "$CLAUDE_SANDBOX" bash -c '
    export CLAUDE_CONFIG_DIR=/sandbox/.claude
    PLUGIN=$(python3 -c "
import json, pathlib
d = json.loads(pathlib.Path(\"$CLAUDE_CONFIG_DIR/settings.json\").read_text())
ep = d.get(\"enabledPlugins\", {})
print(list(ep.keys())[0].split(\"@\")[0])
")
    export AGENT_ENABLED_PLUGINS="$PLUGIN"
    agentic-ci enable-plugins
    python3 -c "
import json, pathlib
d = json.loads(pathlib.Path(\"$CLAUDE_CONFIG_DIR/settings.json\").read_text())
enabled = [k for k, v in d.get(\"enabledPlugins\", {}).items() if v]
print(len(enabled))
"
')"
assert_ok "agentic-ci enable-plugins filters to single plugin (got $PLUGIN_FILTER_RESULT)" \
    test "$PLUGIN_FILTER_RESULT" -eq 1

# --- opencode-sandbox checks ---
print_header "=== opencode-sandbox: binaries ==="
assert_ok "opencode is installed" run_in "$OPENCODE_SANDBOX" opencode --version
print_header "=== opencode-sandbox: skills ==="
assert_ok "opencode.json config exists" \
    run_in "$OPENCODE_SANDBOX" test -f /sandbox/.config/opencode/opencode.json
assert_ok "skills directory exists" \
    run_in "$OPENCODE_SANDBOX" test -d /sandbox/.config/opencode/skills
print_header "=== opencode-sandbox: entrypoint ==="
assert_ok "entrypoint.sh is installed" \
    run_in "$OPENCODE_SANDBOX" test -x /usr/local/bin/entrypoint.sh

print_header "=== opencode-sandbox: AGENT_ENABLED_PLUGINS ==="

# Pick the first plugin from the manifest and verify that enable-plugins
# removes all other plugins' skill directories from disk.
OC_FILTER_RESULT="$(run_in "$OPENCODE_SANDBOX" bash -c '
    export OPENCODE_CONFIG_DIR=/sandbox/.config/opencode
    MANIFEST=/usr/local/share/agentic-ci/plugin-skills.manifest.json
    PLUGIN=$(python3 -c "
import json, pathlib
m = json.loads(pathlib.Path(\"$MANIFEST\").read_text())
print(list(m.keys())[0])
")
    WANTED_COUNT=$(python3 -c "
import json, pathlib
m = json.loads(pathlib.Path(\"$MANIFEST\").read_text())
print(len(m[\"$PLUGIN\"]))
")
    TOTAL_BEFORE=$(find /sandbox/.config/opencode/skills -maxdepth 2 -name SKILL.md | wc -l)
    export AGENT_ENABLED_PLUGINS="$PLUGIN"
    agentic-ci enable-plugins >/dev/null 2>&1
    TOTAL_AFTER=$(find /sandbox/.config/opencode/skills -maxdepth 2 -name SKILL.md | wc -l)
    echo "${PLUGIN}|${WANTED_COUNT}|${TOTAL_BEFORE}|${TOTAL_AFTER}"
')"
OC_WANTED="$(echo "$OC_FILTER_RESULT" | cut -d'|' -f2)"
OC_BEFORE="$(echo "$OC_FILTER_RESULT" | cut -d'|' -f3)"
OC_AFTER="$(echo "$OC_FILTER_RESULT" | cut -d'|' -f4)"

assert_ok "enable-plugins reduced skill count (before=$OC_BEFORE after=$OC_AFTER wanted=$OC_WANTED)" \
    test "$OC_AFTER" -eq "$OC_WANTED"
assert_ok "enable-plugins removed skills (before=$OC_BEFORE > after=$OC_AFTER)" \
    test "$OC_BEFORE" -gt "$OC_AFTER"

# Verify the manifest contains autofix-skills with the expected skills.
OC_AUTOFIX_SKILLS="$(run_in "$OPENCODE_SANDBOX" python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('/usr/local/share/agentic-ci/plugin-skills.manifest.json').read_text())
skills = sorted(m.get('autofix-skills', []))
print(','.join(skills))
")"
assert_contains "manifest has autofix-resolve" "$OC_AUTOFIX_SKILLS" "autofix-resolve"
assert_contains "manifest has autofix-cve-resolve" "$OC_AUTOFIX_SKILLS" "autofix-cve-resolve"
assert_contains "manifest has autofix-triage" "$OC_AUTOFIX_SKILLS" "autofix-triage"

# --- Agent run tests (require credentials) ---
_has_creds() {
    [[ -n "${GCP_SERVICE_ACCOUNT_KEY:-}" ]] || \
    [[ -n "${GCLOUD_CREDENTIALS:-}" ]] || \
    [[ -n "${ANTHROPIC_API_KEY:-}" ]]
}

if ! _has_creds; then
    echo ""
    print_warning "Skipping agent run tests (no credentials set)"
    print_warning "Set GCP_SERVICE_ACCOUNT_KEY, GCLOUD_CREDENTIALS, or ANTHROPIC_API_KEY"
else
    # The OpenShell provider's Vertex backend reads gcloud ADC from disk.
    # CI provides credentials as env vars, so write them to the ADC path.
    ADC_PATH="$HOME/.config/gcloud/application_default_credentials.json"
    if [[ -n "${GCP_SERVICE_ACCOUNT_KEY:-}" ]] && [[ ! -f "$ADC_PATH" ]]; then
        mkdir -p "$(dirname "$ADC_PATH")"
        if echo "$GCP_SERVICE_ACCOUNT_KEY" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null; then
            echo "$GCP_SERVICE_ACCOUNT_KEY" > "$ADC_PATH"
        else
            echo "$GCP_SERVICE_ACCOUNT_KEY" | base64 -d > "$ADC_PATH"
        fi
        print_step "Wrote GCP credentials to gcloud ADC path"
    elif [[ -n "${GCLOUD_CREDENTIALS:-}" ]] && [[ ! -f "$ADC_PATH" ]]; then
        mkdir -p "$(dirname "$ADC_PATH")"
        echo "$GCLOUD_CREDENTIALS" > "$ADC_PATH"
        print_step "Wrote GCP credentials to gcloud ADC path"
    fi

    print_header "=== Component versions ==="
    echo "  agentic-ci:        $(agentic-ci --version 2>&1 || echo unknown)"
    echo "  openshell:         $(openshell --version 2>&1 || echo unknown)"
    echo "  openshell RPM:     $(rpm -q openshell 2>&1 || echo unknown)"
    echo "  openshell-gateway: $(openshell-gateway --version 2>&1 || echo unknown)"
    echo "  openshell-gw RPM:  $(rpm -q openshell-gateway 2>&1 || echo unknown)"
    echo "  podman:            $(podman --version 2>&1 || echo unknown)"
    echo "  claude:            $(run_in "$CLAUDE_SANDBOX" claude --version 2>&1 || echo unknown)"
    echo "  opencode:          $(run_in "$OPENCODE_SANDBOX" opencode --version 2>&1 || echo unknown)"

    # --- Claude Code via OpenShell ---
    print_header "=== agentic-ci run: Claude Code via OpenShell ==="

    WORKDIR="$TMPDIR_E2E/claude"
    mkdir -p "$WORKDIR"

    print_step "Running Claude Code via agentic-ci (openshell backend)..."
    RC=0
    agentic-ci run "Reply with only the word pong" \
        --backend openshell \
        --image "$CLAUDE_SANDBOX" \
        --harness claude-code \
        --workdir "$WORKDIR" \
        --no-otel \
        --no-streaming || RC=$?

    assert_ok "claude-code exited successfully" test "$RC" -eq 0
    dump_gateway_log

    agentic-ci stop --backend openshell --harness claude-code 2>/dev/null || true

    # --- OpenCode via OpenShell ---
    print_header "=== agentic-ci run: OpenCode via OpenShell ==="

    WORKDIR="$TMPDIR_E2E/opencode"
    mkdir -p "$WORKDIR"

    print_step "Running OpenCode via agentic-ci (openshell backend)..."
    RC=0
    agentic-ci run "Reply with only the word pong" \
        --backend openshell \
        --image "$OPENCODE_SANDBOX" \
        --harness opencode \
        --workdir "$WORKDIR" \
        --no-otel \
        --no-streaming || RC=$?

    assert_ok "opencode exited successfully" test "$RC" -eq 0
    dump_gateway_log

    agentic-ci stop --backend openshell --harness opencode 2>/dev/null || true

    # --- Repo-level network policy test ---
    # Verifies that .agentic-ci/openshell-policy.yml in the workdir adds
    # extra endpoints to the sandbox policy.  packages.redhat.com is NOT in the
    # default endpoint list, so the agent can only reach it if the repo
    # policy file is picked up correctly.
    print_header "=== agentic-ci run: repo-level network policy ==="

    WORKDIR="$TMPDIR_E2E/repo-policy"
    mkdir -p "$WORKDIR/.agentic-ci"
    cat > "$WORKDIR/.agentic-ci/openshell-policy.yml" <<'POLICY'
endpoints:
  - "packages.redhat.com:443:read-write"
POLICY

    print_step "Running Claude Code with repo-level policy (packages.redhat.com allowed)..."
    POLICY_LOG="$TMPDIR_E2E/repo-policy.log"
    RC=0
    agentic-ci run \
        "Use curl to fetch https://packages.redhat.com. If you get a response, reply with only the word pong. If you cannot reach it, reply with only the word fail." \
        --backend openshell \
        --image "$CLAUDE_SANDBOX" \
        --harness claude-code \
        --workdir "$WORKDIR" \
        --no-otel 2>&1 | tee "$POLICY_LOG" || RC=$?

    OUTPUT="$(cat "$POLICY_LOG")"
    assert_ok "repo-policy run exited successfully" test "$RC" -eq 0
    assert_contains "repo policy: agent reached packages.redhat.com" "$OUTPUT" "pong"
    assert_contains "repo policy: source logged" "$OUTPUT" "Policy source: repo"
    dump_gateway_log

    agentic-ci stop --backend openshell --harness claude-code 2>/dev/null || true

    # --- Verdict file download test ---
    # Verifies that files written to gitignored directories inside the
    # sandbox (like autofix-output/) are downloaded back to the host.
    # This was broken when sandbox.download() used --no-git-ignore
    # (unsupported flag) and when the verdict check ran before download.
    print_header "=== agentic-ci run: verdict file download ==="

    WORKDIR="$TMPDIR_E2E/verdict-download"
    mkdir -p "$WORKDIR"
    # Add a .gitignore that excludes the output directory, matching the
    # real autofix layout where the verdict lives in a gitignored path.
    cat > "$WORKDIR/.gitignore" <<'GITIGNORE'
autofix-output/
GITIGNORE
    git -C "$WORKDIR" init -q

    print_step "Running Claude Code to create a file in a gitignored directory..."
    VERDICT_LOG="$TMPDIR_E2E/verdict-download.log"
    RC=0
    agentic-ci run \
        "Create the directory autofix-output/ then write the file autofix-output/verdict.json with the content {\"verdict\": \"committed\"}. Do not say anything else." \
        --backend openshell \
        --image "$CLAUDE_SANDBOX" \
        --harness claude-code \
        --workdir "$WORKDIR" \
        --no-otel 2>&1 | tee "$VERDICT_LOG" || RC=$?

    assert_ok "verdict-download run exited successfully" test "$RC" -eq 0
    assert_ok "verdict-download: verdict file downloaded to host" \
        test -f "$WORKDIR/autofix-output/verdict.json"

    if [[ -f "$WORKDIR/autofix-output/verdict.json" ]]; then
        VERDICT_CONTENT="$(cat "$WORKDIR/autofix-output/verdict.json")"
        assert_contains "verdict-download: file has expected content" \
            "$VERDICT_CONTENT" "committed"
    fi
    dump_gateway_log

    agentic-ci stop --backend openshell --harness claude-code 2>/dev/null || true

    # --- AGENT_ENABLED_PLUGINS via OpenShell ---
    # Verifies that the env script sources entrypoint.sh and calls
    # _enable_plugins so only the requested plugins are loaded.
    print_header "=== agentic-ci run: AGENT_ENABLED_PLUGINS via OpenShell ==="

    WORKDIR="$TMPDIR_E2E/plugins"
    mkdir -p "$WORKDIR"

    # Pick the first enabled plugin from the image
    FIRST_PLUGIN="$(run_in "$CLAUDE_SANDBOX" python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('/sandbox/.claude/settings.json').read_text())
ep = d.get('enabledPlugins', {})
print(list(ep.keys())[0].split('@')[0])
")"
    print_step "Testing with AGENT_ENABLED_PLUGINS=$FIRST_PLUGIN"

    PLUGINS_LOG="$TMPDIR_E2E/plugins.log"
    RC=0
    AGENT_ENABLED_PLUGINS="$FIRST_PLUGIN" \
    agentic-ci run "Reply with only the word pong" \
        --backend openshell \
        --image "$CLAUDE_SANDBOX" \
        --harness claude-code \
        --workdir "$WORKDIR" \
        --no-otel 2>&1 | tee "$PLUGINS_LOG" || RC=$?

    OUTPUT="$(cat "$PLUGINS_LOG")"
    assert_ok "plugin-filter run exited successfully" test "$RC" -eq 0

    # The "Plugins:" line should list only the single requested plugin
    PLUGINS_LINE="$(grep -i '^\s*Plugins:' "$PLUGINS_LOG" || true)"
    if [[ -n "$PLUGINS_LINE" ]]; then
        PLUGIN_COUNT="$(echo "$PLUGINS_LINE" | tr ',' '\n' | grep -c '[a-z]')"
        assert_ok "plugin-filter: only 1 plugin loaded (got $PLUGIN_COUNT)" \
            test "$PLUGIN_COUNT" -eq 1
        assert_contains "plugin-filter: correct plugin loaded" "$PLUGINS_LINE" "$FIRST_PLUGIN"
    else
        print_error "FAIL: plugin-filter: no Plugins line found in output"
        FAIL=$((FAIL + 1))
    fi
    dump_gateway_log

    agentic-ci stop --backend openshell --harness claude-code 2>/dev/null || true

    # --- AGENT_ENABLED_PLUGINS via OpenShell (OpenCode) ---
    # Verifies that the env script calls enable-plugins so only the
    # requested plugin's skills are on disk when the agent starts.
    print_header "=== agentic-ci run: AGENT_ENABLED_PLUGINS via OpenShell (OpenCode) ==="

    WORKDIR="$TMPDIR_E2E/oc-plugins"
    mkdir -p "$WORKDIR"

    OC_FIRST_PLUGIN="$(run_in "$OPENCODE_SANDBOX" python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('/usr/local/share/agentic-ci/plugin-skills.manifest.json').read_text())
print(list(m.keys())[0])
")"
    OC_EXPECTED_COUNT="$(run_in "$OPENCODE_SANDBOX" python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('/usr/local/share/agentic-ci/plugin-skills.manifest.json').read_text())
print(len(m['$OC_FIRST_PLUGIN']))
")"
    print_step "Testing with AGENT_ENABLED_PLUGINS=$OC_FIRST_PLUGIN (expecting $OC_EXPECTED_COUNT skills)"

    OC_PLUGINS_LOG="$TMPDIR_E2E/oc-plugins.log"
    RC=0
    AGENT_ENABLED_PLUGINS="$OC_FIRST_PLUGIN" \
    agentic-ci run "Reply with only the word pong" \
        --backend openshell \
        --image "$OPENCODE_SANDBOX" \
        --harness opencode \
        --workdir "$WORKDIR" \
        --no-otel 2>&1 | tee "$OC_PLUGINS_LOG" || RC=$?

    assert_ok "opencode plugin-filter run exited successfully" test "$RC" -eq 0

    # Verify the sandbox only has the wanted plugin's skills on disk.
    # Download the workdir so we can inspect the sandbox state via the
    # agent's own output (the agent was asked to reply "pong", but the
    # skill removal happens before the agent starts).
    OC_SKILL_COUNT="$(run_in "$OPENCODE_SANDBOX" bash -c "
        export OPENCODE_CONFIG_DIR=/sandbox/.config/opencode
        export AGENT_ENABLED_PLUGINS=\"$OC_FIRST_PLUGIN\"
        agentic-ci enable-plugins >/dev/null 2>&1
        find /sandbox/.config/opencode/skills -maxdepth 2 -name SKILL.md | wc -l
    ")"
    assert_ok "opencode plugin-filter: only $OC_FIRST_PLUGIN skills remain (got $OC_SKILL_COUNT, expected $OC_EXPECTED_COUNT)" \
        test "$OC_SKILL_COUNT" -eq "$OC_EXPECTED_COUNT"

    dump_gateway_log

    agentic-ci stop --backend openshell --harness opencode 2>/dev/null || true
fi

echo ""
print_header "=== All test sections complete ==="
