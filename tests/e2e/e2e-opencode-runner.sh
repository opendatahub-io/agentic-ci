#!/bin/bash
# shellcheck source=tests/images/shell-utils.sh
# e2e-opencode-runner.sh -- End-to-end tests for the opencode-runner image
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
#   ./tests/e2e/e2e-opencode-runner.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/../images/shell-utils.sh"

PASS=0
FAIL=0
TMPDIR_E2E="$(mktemp -d)"

cleanup() {
    agentic-ci stop --harness opencode 2>/dev/null || true
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

IMAGE="${OPENCODE_CONTAINER_IMAGE:-localhost/opencode-runner:latest}"

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

# -- Run OpenCode test --------------------------------------------------------
print_header "=== agentic-ci run: OpenCode ==="

WORKDIR="$TMPDIR_E2E/run"
mkdir -p "$WORKDIR"

print_step "Running OpenCode via agentic-ci..."
RC=0
agentic-ci run "Reply with only the word pong" \
    --image "$IMAGE" \
    --harness opencode \
    --workdir "$WORKDIR" \
    --no-otel \
    --no-streaming \
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
agentic-ci stop --harness opencode 2>/dev/null || true

# -- AGENT_ENABLED_PLUGINS test -----------------------------------------------
print_header "=== agentic-ci run: AGENT_ENABLED_PLUGINS ==="

WORKDIR="$TMPDIR_E2E/plugins"
mkdir -p "$WORKDIR"

MANIFEST="/usr/local/share/agentic-ci/plugin-skills.manifest.json"
OC_FIRST_PLUGIN="$(podman run --rm --entrypoint "" "$IMAGE" python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('$MANIFEST').read_text())
print(list(m.keys())[0])
")"
OC_EXPECTED_COUNT="$(podman run --rm --entrypoint "" "$IMAGE" python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('$MANIFEST').read_text())
print(len(m['$OC_FIRST_PLUGIN']))
")"
print_step "Testing with AGENT_ENABLED_PLUGINS=$OC_FIRST_PLUGIN (expecting $OC_EXPECTED_COUNT skills)"

RC=0
AGENT_ENABLED_PLUGINS="$OC_FIRST_PLUGIN" \
agentic-ci run "Reply with only the word pong" \
    --image "$IMAGE" \
    --harness opencode \
    --workdir "$WORKDIR" \
    --no-otel \
    --no-streaming \
    > "$TMPDIR_E2E/plugins-out.txt" 2>"$TMPDIR_E2E/plugins-err.txt" || RC=$?

assert_ok "plugin-filter run exited successfully" test "$RC" -eq 0

# Verify the container only has the wanted plugin's skills on disk.
# Run a fresh container with the same filtering to check the result.
OC_SKILL_COUNT="$(podman run --rm --entrypoint "" \
    -e OPENCODE_CONFIG_DIR=/home/agent-ci/.config/opencode \
    -e AGENT_ENABLED_PLUGINS="$OC_FIRST_PLUGIN" \
    -e AGENT_TOOL=opencode \
    "$IMAGE" bash -c "
        agentic-ci enable-plugins >/dev/null 2>&1
        find /home/agent-ci/.config/opencode/skills -maxdepth 2 -name SKILL.md | wc -l
    ")"
assert_ok "plugin-filter: only $OC_FIRST_PLUGIN skills remain (got $OC_SKILL_COUNT, expected $OC_EXPECTED_COUNT)" \
    test "$OC_SKILL_COUNT" -eq "$OC_EXPECTED_COUNT"

echo ""
print_header "=== All test sections complete ==="
