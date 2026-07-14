#!/bin/bash
# shellcheck source=tests/images/shell-utils.sh
# e2e-claude-runner.sh -- End-to-end tests for the claude-runner image
# using agentic-ci.
#
# Builds are handled by the CI job; this script runs a test prompt via
# agentic-ci and verifies the output.
#
# Requires: python3, podman, agentic-ci
# Credentials: GCP_SERVICE_ACCOUNT_KEY, GCLOUD_CREDENTIALS,
#              or ANTHROPIC_API_KEY
#
# Usage:
#   ./tests/e2e/e2e-claude-runner.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/../images/shell-utils.sh"

PASS=0
FAIL=0
TMPDIR_E2E="$(mktemp -d)"

cleanup() {
    agentic-ci stop --harness claude-code 2>/dev/null || true
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

# -- Preflight ---------------------------------------------------------------
print_header "=== Preflight checks ==="
check_dependencies python3 podman agentic-ci

print_header "=== Component versions ==="
echo "  agentic-ci: $(agentic-ci --version 2>&1 || echo unknown)"
echo "  podman:     $(podman --version 2>&1 || echo unknown)"

IMAGE="${CLAUDE_CONTAINER_IMAGE:-localhost/claude-runner:latest}"

_has_creds() {
    [[ -n "${GCP_SERVICE_ACCOUNT_KEY:-}" ]] || \
    [[ -n "${GCLOUD_CREDENTIALS:-}" ]] || \
    [[ -n "${ANTHROPIC_API_KEY:-}" ]]
}

if ! _has_creds; then
    echo ""
    print_warning "Skipping e2e tests (no credentials set)"
    print_warning "Set GCP_SERVICE_ACCOUNT_KEY, GCLOUD_CREDENTIALS, or ANTHROPIC_API_KEY"
    exit 0
fi

# -- Streaming test -----------------------------------------------------------
print_header "=== agentic-ci run: streaming ==="

WORKDIR="$TMPDIR_E2E/streaming"
mkdir -p "$WORKDIR"

print_step "Running Claude Code via agentic-ci (streaming)..."
RC=0
agentic-ci run "Reply with only the word pong" \
    --image "$IMAGE" \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    > "$TMPDIR_E2E/stream-out.txt" 2>"$TMPDIR_E2E/stream-err.txt" || RC=$?

assert_ok "streaming container exited successfully" test "$RC" -eq 0
assert_ok "streaming output file is non-empty" test -s "$TMPDIR_E2E/stream-out.txt"
assert_contains "streaming output contains response" \
    "$(cat "$TMPDIR_E2E/stream-out.txt")" "pong"

if [[ -s "$TMPDIR_E2E/stream-out.txt" ]]; then
    echo "--- streaming output ---"
    head -20 "$TMPDIR_E2E/stream-out.txt"
    echo "--- end streaming output ---"
fi

# Stop the container before the next test
agentic-ci stop --harness claude-code 2>/dev/null || true

# -- Non-streaming test -------------------------------------------------------
print_header "=== agentic-ci run: non-streaming ==="

WORKDIR="$TMPDIR_E2E/nostream"
mkdir -p "$WORKDIR"

print_step "Running Claude Code via agentic-ci (non-streaming)..."
RC=0
agentic-ci run "Reply with only the word pong" \
    --image "$IMAGE" \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    --no-streaming \
    > "$TMPDIR_E2E/nostream-out.txt" 2>"$TMPDIR_E2E/nostream-err.txt" || RC=$?

assert_ok "non-streaming container exited successfully" test "$RC" -eq 0
assert_ok "non-streaming output file is non-empty" test -s "$TMPDIR_E2E/nostream-out.txt"
assert_contains "non-streaming output contains response" \
    "$(cat "$TMPDIR_E2E/nostream-out.txt")" "pong"

if [[ -s "$TMPDIR_E2E/nostream-out.txt" ]]; then
    echo "--- non-streaming output ---"
    head -20 "$TMPDIR_E2E/nostream-out.txt"
    echo "--- end non-streaming output ---"
fi

# Stop the container before the next test
agentic-ci stop --harness claude-code 2>/dev/null || true

# -- Setup steps test ---------------------------------------------------------
print_header "=== agentic-ci run: setup steps (podman) ==="

WORKDIR="$TMPDIR_E2E/setup-steps"
mkdir -p "$WORKDIR/.agentic-ci"
cat > "$WORKDIR/.agentic-ci/config.yml" <<'CONFIG'
setup:
  - name: Create marker file
    run: echo "setup-complete" > .setup-marker
CONFIG

print_step "Running Claude Code with setup steps (podman)..."
SETUP_LOG="$TMPDIR_E2E/setup-steps-out.txt"
RC=0
agentic-ci run \
    "Check if the file .setup-marker exists and contains 'setup-complete'. If yes, reply with only the word pong. If not, reply with only the word fail." \
    --image "$IMAGE" \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    --no-streaming \
    > "$SETUP_LOG" 2>&1 || RC=$?

OUTPUT="$(cat "$SETUP_LOG")"
assert_ok "setup-steps run exited successfully" test "$RC" -eq 0
assert_contains "setup-steps: marker file found by agent" "$OUTPUT" "pong"

agentic-ci stop --harness claude-code 2>/dev/null || true

# -- AGENT_ENABLED_PLUGINS test -----------------------------------------------
print_header "=== agentic-ci run: AGENT_ENABLED_PLUGINS ==="

WORKDIR="$TMPDIR_E2E/plugins"
mkdir -p "$WORKDIR"

# Pick the first enabled plugin from the image
FIRST_PLUGIN="$(podman run --rm --entrypoint "" "$IMAGE" python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('/home/agent-ci/.claude/settings.json').read_text())
ep = d.get('enabledPlugins', {})
print(list(ep.keys())[0].split('@')[0])
")"
print_step "Testing with AGENT_ENABLED_PLUGINS=$FIRST_PLUGIN"

RC=0
AGENT_ENABLED_PLUGINS="$FIRST_PLUGIN" \
agentic-ci run "Reply with only the word pong" \
    --image "$IMAGE" \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    --no-streaming \
    > "$TMPDIR_E2E/plugins-out.txt" 2>&1 || RC=$?

assert_ok "plugin-filter run exited successfully" test "$RC" -eq 0

# Verify settings.json has only the wanted plugin enabled.
# Run a fresh container with the same env to check the entrypoint result.
CC_ENABLED_COUNT="$(podman run --rm --entrypoint "" \
    -e CLAUDE_CONFIG_DIR=/home/agent-ci/.claude \
    -e AGENT_ENABLED_PLUGINS="$FIRST_PLUGIN" \
    -e AGENT_TOOL=claude \
    "$IMAGE" bash -c "
        agentic-ci enable-plugins >/dev/null 2>&1
        python3 -c \"
import json, pathlib
d = json.loads(pathlib.Path('/home/agent-ci/.claude/settings.json').read_text())
enabled = [k for k, v in d.get('enabledPlugins', {}).items() if v]
print(len(enabled))
\"
    ")"
assert_ok "plugin-filter: only 1 plugin enabled (got $CC_ENABLED_COUNT)" \
    test "$CC_ENABLED_COUNT" -eq 1

echo ""
print_header "=== All test sections complete ==="
