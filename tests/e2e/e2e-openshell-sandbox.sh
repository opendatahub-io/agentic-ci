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

# Pre-built supervisor from PR #1763 (google-cloud provider + GCE metadata
# emulator). The upstream supervisor image doesn't include these changes
# yet. Remove this override once PR #1763 merges and the openshell RPM
# is upgraded past 0.0.55.
# Tracking: https://github.com/NVIDIA/OpenShell/pull/1763
export OPENSHELL_SUPERVISOR_IMAGE=quay.io/mprpic/openshell-supervisor:pr1763
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

print_header "=== claude-sandbox: plugins ==="

assert_ok "settings.json exists" \
    run_in "$CLAUDE_SANDBOX" test -f /sandbox/.claude/settings.json
assert_ok "installed_plugins.json exists" \
    run_in "$CLAUDE_SANDBOX" test -f /sandbox/.claude/plugins/installed_plugins.json
assert_ok "marketplace registered" \
    run_in "$CLAUDE_SANDBOX" test -d /sandbox/.claude/plugins/marketplaces

print_header "=== claude-sandbox: entrypoint ==="

assert_ok "entrypoint.sh is installed" \
    run_in "$CLAUDE_SANDBOX" test -x /usr/local/bin/entrypoint.sh

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
fi

echo ""
print_header "=== All test sections complete ==="
