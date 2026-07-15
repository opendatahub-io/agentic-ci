#!/bin/bash
# shellcheck source=tests/images/shell-utils.sh
# e2e-local-runner.sh -- End-to-end tests for the local backend
# using agentic-ci.
#
# The local backend runs the agent binary directly without a container.
# This test verifies streaming, non-streaming, and extra args passthrough.
#
# Requires: python3, claude, agentic-ci
# Credentials: ANTHROPIC_API_KEY or Vertex AI (GCP_SERVICE_ACCOUNT_KEY, etc.)
#
# Usage:
#   ./tests/e2e/e2e-local-runner.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/../images/shell-utils.sh"

PASS=0
FAIL=0
TMPDIR_E2E="$(mktemp -d)"

cleanup() {
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
check_dependencies python3 claude agentic-ci

print_header "=== Component versions ==="
echo "  agentic-ci: $(agentic-ci --version 2>&1 || echo unknown)"
echo "  claude:     $(claude --version 2>&1 || echo unknown)"

_has_creds() {
    [[ -n "${GCP_SERVICE_ACCOUNT_KEY:-}" ]] || \
    [[ -n "${GCLOUD_CREDENTIALS:-}" ]] || \
    [[ -n "${ANTHROPIC_API_KEY:-}" ]] || \
    [[ -f "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]] || \
    [[ -f "${HOME}/.config/gcloud/application_default_credentials.json" ]]
}

if ! _has_creds; then
    echo ""
    print_warning "Skipping e2e tests (no credentials set)"
    print_warning "Set GCP_SERVICE_ACCOUNT_KEY, GCLOUD_CREDENTIALS, ANTHROPIC_API_KEY, or configure gcloud ADC"
    exit 0
fi

# -- Streaming test -----------------------------------------------------------
print_header "=== agentic-ci run --backend local: streaming ==="

WORKDIR="$TMPDIR_E2E/streaming"
mkdir -p "$WORKDIR"

print_step "Running Claude Code via local backend (streaming)..."
RC=0
agentic-ci run --backend local \
    "Reply with only the word pong" \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    > "$TMPDIR_E2E/stream-out.txt" 2>"$TMPDIR_E2E/stream-err.txt" || RC=$?

assert_ok "streaming local run exited successfully" test "$RC" -eq 0
assert_ok "streaming output file is non-empty" test -s "$TMPDIR_E2E/stream-out.txt"
assert_contains "streaming output contains response" \
    "$(cat "$TMPDIR_E2E/stream-out.txt")" "pong"

if [[ -s "$TMPDIR_E2E/stream-out.txt" ]]; then
    echo "--- streaming output ---"
    head -20 "$TMPDIR_E2E/stream-out.txt"
    echo "--- end streaming output ---"
fi

# -- Non-streaming test -------------------------------------------------------
print_header "=== agentic-ci run --backend local: non-streaming ==="

WORKDIR="$TMPDIR_E2E/nostream"
mkdir -p "$WORKDIR"

print_step "Running Claude Code via local backend (non-streaming)..."
RC=0
agentic-ci run --backend local \
    "Reply with only the word pong" \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    --no-streaming \
    > "$TMPDIR_E2E/nostream-out.txt" 2>"$TMPDIR_E2E/nostream-err.txt" || RC=$?

assert_ok "non-streaming local run exited successfully" test "$RC" -eq 0
assert_ok "non-streaming output file is non-empty" test -s "$TMPDIR_E2E/nostream-out.txt"
assert_contains "non-streaming output contains response" \
    "$(cat "$TMPDIR_E2E/nostream-out.txt")" "pong"

if [[ -s "$TMPDIR_E2E/nostream-out.txt" ]]; then
    echo "--- non-streaming output ---"
    head -20 "$TMPDIR_E2E/nostream-out.txt"
    echo "--- end non-streaming output ---"
fi

# -- Extra args passthrough test ----------------------------------------------
print_header "=== agentic-ci run --backend local: extra args ==="

WORKDIR="$TMPDIR_E2E/extra-args"
mkdir -p "$WORKDIR"

print_step "Running Claude Code via local backend (extra args: --max-turns 5)..."
RC=0
agentic-ci run --backend local \
    "Reply with only the word pong" \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    -- --max-turns 5 --verbose \
    > "$TMPDIR_E2E/extra-args-out.txt" 2>"$TMPDIR_E2E/extra-args-err.txt" || RC=$?

assert_ok "extra args local run exited successfully" test "$RC" -eq 0
assert_ok "extra args output file is non-empty" test -s "$TMPDIR_E2E/extra-args-out.txt"
assert_contains "extra args output contains response" \
    "$(cat "$TMPDIR_E2E/extra-args-out.txt")" "pong"

# -- Setup steps test ---------------------------------------------------------
print_header "=== agentic-ci run --backend local: setup steps ==="

WORKDIR="$TMPDIR_E2E/setup-steps"
mkdir -p "$WORKDIR/.agentic-ci"
cat > "$WORKDIR/.agentic-ci/config.yml" <<'CONFIG'
setup:
  - name: Create marker file
    run: echo "setup-complete" > .setup-marker
CONFIG

print_step "Running Claude Code with setup steps (local)..."
RC=0
agentic-ci run --backend local \
    "Check if the file .setup-marker exists and contains 'setup-complete'. If yes, reply with only the word pong. If not, reply with only the word fail." \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    --no-streaming \
    > "$TMPDIR_E2E/setup-out.txt" 2>"$TMPDIR_E2E/setup-err.txt" || RC=$?

assert_ok "setup-steps local run exited successfully" test "$RC" -eq 0
assert_contains "setup-steps: marker file found by agent" \
    "$(cat "$TMPDIR_E2E/setup-out.txt")" "pong"

# -- AGENTIC_CI_SKIP_SETUP test -----------------------------------------------
print_header "=== agentic-ci run --backend local: AGENTIC_CI_SKIP_SETUP ==="

WORKDIR="$TMPDIR_E2E/skip-setup"
mkdir -p "$WORKDIR/.agentic-ci"
cat > "$WORKDIR/.agentic-ci/config.yml" <<'CONFIG'
setup:
  - name: Create marker file
    run: echo "setup-complete" > .setup-marker
CONFIG

print_step "Running Claude Code with AGENTIC_CI_SKIP_SETUP=1 (local)..."
RC=0
AGENTIC_CI_SKIP_SETUP=1 \
agentic-ci run --backend local \
    "Check if the file .setup-marker exists. If yes, reply with only the word fail. If not, reply with only the word pong." \
    --harness claude-code \
    --workdir "$WORKDIR" \
    --no-otel \
    --no-streaming \
    > "$TMPDIR_E2E/skip-setup-out.txt" 2>"$TMPDIR_E2E/skip-setup-err.txt" || RC=$?

assert_ok "skip-setup local run exited successfully" test "$RC" -eq 0
assert_contains "skip-setup: marker file NOT created" \
    "$(cat "$TMPDIR_E2E/skip-setup-out.txt")" "pong"

echo ""
print_header "=== All test sections complete ==="
