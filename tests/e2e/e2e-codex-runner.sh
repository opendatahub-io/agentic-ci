#!/bin/bash
# shellcheck source=tests/images/shell-utils.sh
# e2e-codex-runner.sh -- End-to-end tests for the codex-runner image
# using agentic-ci.
#
# Builds are handled by the CI job; this script runs a test prompt via
# agentic-ci and verifies the output.
#
# Requires: python3, podman, agentic-ci
# Credentials: OPENAI_API_KEY
#
# Usage:
#   ./tests/e2e/e2e-codex-runner.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/../images/shell-utils.sh"

PASS=0
FAIL=0
TMPDIR_E2E="$(mktemp -d)"

cleanup() {
    agentic-ci stop --harness codex 2>/dev/null || true
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

IMAGE="${CODEX_CONTAINER_IMAGE:-localhost/codex-runner:latest}"

_has_creds() {
    [[ -n "${OPENAI_API_KEY:-}" ]]
}

if ! _has_creds; then
    echo ""
    print_warning "Skipping e2e tests (no credentials set)"
    print_warning "Set OPENAI_API_KEY"
    exit 0
fi

# -- Run Codex test -----------------------------------------------------------
print_header "=== agentic-ci run: Codex ==="

WORKDIR="$TMPDIR_E2E/run"
mkdir -p "$WORKDIR"

print_step "Running Codex via agentic-ci..."
RC=0
agentic-ci run "Reply with only the word pong" \
    --image "$IMAGE" \
    --harness codex \
    --workdir "$WORKDIR" \
    --no-otel \
    > "$TMPDIR_E2E/out.txt" 2>"$TMPDIR_E2E/err.txt" || RC=$?

assert_ok "container exited successfully" test "$RC" -eq 0

COMBINED_OUTPUT="$(cat "$TMPDIR_E2E/out.txt" 2>/dev/null)$(cat "$TMPDIR_E2E/err.txt" 2>/dev/null)"
assert_ok "output captured" test -n "$COMBINED_OUTPUT"

if [[ -s "$TMPDIR_E2E/out.txt" ]]; then
    echo "--- output ---"
    head -20 "$TMPDIR_E2E/out.txt"
    echo "--- end output ---"
fi
if [[ -s "$TMPDIR_E2E/err.txt" ]]; then
    echo "--- stderr ---"
    head -20 "$TMPDIR_E2E/err.txt"
    echo "--- end stderr ---"
fi

# Stop the container before the next test
agentic-ci stop --harness codex 2>/dev/null || true

# -- Setup steps test ---------------------------------------------------------
print_header "=== agentic-ci run: setup steps (podman) ==="

WORKDIR="$TMPDIR_E2E/setup-steps"
mkdir -p "$WORKDIR/.agentic-ci"
cat > "$WORKDIR/.agentic-ci/config.yml" <<'CONFIG'
setup:
  - name: Create marker file
    run: echo "setup-complete" > .setup-marker
CONFIG

print_step "Running Codex with setup steps (podman)..."
SETUP_LOG="$TMPDIR_E2E/setup-steps-out.txt"
RC=0
agentic-ci run \
    "Check if the file .setup-marker exists and contains 'setup-complete'. If yes, reply with only the word pong. If not, reply with only the word fail." \
    --image "$IMAGE" \
    --harness codex \
    --workdir "$WORKDIR" \
    --no-otel \
    > "$SETUP_LOG" 2>&1 || RC=$?

OUTPUT="$(cat "$SETUP_LOG")"
assert_ok "setup-steps run exited successfully" test "$RC" -eq 0
assert_contains "setup-steps: marker file found by agent" "$OUTPUT" "pong"

agentic-ci stop --harness codex 2>/dev/null || true

echo ""
print_header "=== All test sections complete ==="
