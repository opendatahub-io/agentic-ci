#!/bin/bash
# test-entrypoint.sh -- Test the shared entrypoint env detection.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

source "$SCRIPT_DIR/shell-utils.sh"

PASS=0
FAIL=0

assert_ok() {
    local desc="$1"; shift
    if "$@"; then
        print_success "PASS: $desc"
        PASS=$((PASS + 1))
    else
        print_error "FAIL: $desc"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local desc="$1" output="$2" pattern="$3"
    if echo "$output" | grep -q "$pattern"; then
        print_success "PASS: $desc"
        PASS=$((PASS + 1))
    else
        print_error "FAIL: $desc — expected '$pattern' in output"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_contains() {
    local desc="$1" output="$2" pattern="$3"
    if ! echo "$output" | grep -q "$pattern"; then
        print_success "PASS: $desc"
        PASS=$((PASS + 1))
    else
        print_error "FAIL: $desc — did not expect '$pattern' in output"
        FAIL=$((FAIL + 1))
    fi
}

print_results() {
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
trap print_results EXIT

ENTRYPOINT="$REPO_ROOT/images/runner/shared/entrypoint.sh"

_DETECT_SCRIPT='
source "'"$ENTRYPOINT"'" --source-only
_detect_tool "$1"
for v in CLAUDE_CODE_USE_VERTEX DISABLE_AUTOUPDATER OPENCODE_DISABLE_AUTOUPDATE CURSOR_DISABLE_AUTOUPDATE; do
    if val=$(printenv "$v" 2>/dev/null); then printf "%s=%s\n" "$v" "$val"; fi
done
'

run_detect() {
    env -i HOME="$HOME" PATH="$PATH" bash -c "$_DETECT_SCRIPT" _ "$1" 2>/dev/null
}

run_detect_with_vertex() {
    env -i HOME="$HOME" PATH="$PATH" bash -c "
        _VERTEX_CONFIGURED=1; $_DETECT_SCRIPT
    " _ "$1" 2>/dev/null
}

print_header "=== Entrypoint tool detection: claude ==="

CLAUDE_ENV=$(run_detect "claude")
assert_contains "claude: sets DISABLE_AUTOUPDATER=1" "$CLAUDE_ENV" "DISABLE_AUTOUPDATER=1"
assert_not_contains "claude: does not set CLAUDE_CODE_USE_VERTEX" "$CLAUDE_ENV" "CLAUDE_CODE_USE_VERTEX"
assert_not_contains "claude: does not set OPENCODE_DISABLE_AUTOUPDATE" "$CLAUDE_ENV" "OPENCODE_DISABLE_AUTOUPDATE"

print_header "=== Entrypoint tool detection: opencode ==="

OC_ENV=$(run_detect "opencode")
assert_contains "opencode: sets OPENCODE_DISABLE_AUTOUPDATE=1" "$OC_ENV" "OPENCODE_DISABLE_AUTOUPDATE=1"
assert_not_contains "opencode: does not set DISABLE_AUTOUPDATER" "$OC_ENV" "DISABLE_AUTOUPDATER"

print_header "=== Entrypoint tool detection: cursor (agent) ==="

CURSOR_ENV=$(run_detect "agent")
assert_contains "agent: sets CURSOR_DISABLE_AUTOUPDATE=1" "$CURSOR_ENV" "CURSOR_DISABLE_AUTOUPDATE=1"
assert_not_contains "agent: does not set DISABLE_AUTOUPDATER" "$CURSOR_ENV" "DISABLE_AUTOUPDATER"
assert_not_contains "agent: does not set OPENCODE_DISABLE_AUTOUPDATE" "$CURSOR_ENV" "OPENCODE_DISABLE_AUTOUPDATE"

CURSOR_ENV2=$(run_detect "cursor")
assert_contains "cursor: sets CURSOR_DISABLE_AUTOUPDATE=1" "$CURSOR_ENV2" "CURSOR_DISABLE_AUTOUPDATE=1"

print_header "=== Entrypoint tool detection: AGENT_TOOL fallback ==="

AGENT_TOOL_CLAUDE_ENV=$(env -i HOME="$HOME" PATH="$PATH" AGENT_TOOL=claude bash -c "
    source '$ENTRYPOINT' --source-only
    _detect_tool 'some-other-cmd'
    printenv DISABLE_AUTOUPDATER 2>/dev/null && echo 'DISABLE_AUTOUPDATER='\$(printenv DISABLE_AUTOUPDATER) || true
" 2>/dev/null)
assert_contains "AGENT_TOOL=claude: sets DISABLE_AUTOUPDATER=1" "$AGENT_TOOL_CLAUDE_ENV" "DISABLE_AUTOUPDATER=1"

AGENT_TOOL_OC_ENV=$(env -i HOME="$HOME" PATH="$PATH" AGENT_TOOL=opencode bash -c "
    source '$ENTRYPOINT' --source-only
    _detect_tool 'some-other-cmd'
    printenv OPENCODE_DISABLE_AUTOUPDATE 2>/dev/null && echo 'OPENCODE_DISABLE_AUTOUPDATE='\$(printenv OPENCODE_DISABLE_AUTOUPDATE) || true
" 2>/dev/null)
assert_contains "AGENT_TOOL=opencode: sets OPENCODE_DISABLE_AUTOUPDATE=1" "$AGENT_TOOL_OC_ENV" "OPENCODE_DISABLE_AUTOUPDATE=1"

AGENT_TOOL_CURSOR_ENV=$(env -i HOME="$HOME" PATH="$PATH" AGENT_TOOL=cursor bash -c "
    source '$ENTRYPOINT' --source-only
    _detect_tool 'some-other-cmd'
    printenv CURSOR_DISABLE_AUTOUPDATE 2>/dev/null && echo 'CURSOR_DISABLE_AUTOUPDATE='\$(printenv CURSOR_DISABLE_AUTOUPDATE) || true
" 2>/dev/null)
assert_contains "AGENT_TOOL=cursor: sets CURSOR_DISABLE_AUTOUPDATE=1" "$AGENT_TOOL_CURSOR_ENV" "CURSOR_DISABLE_AUTOUPDATE=1"

print_header "=== Entrypoint prepend: AGENT_TOOL=cursor maps to agent ==="
# The default branch must exec `agent`, not `cursor` (binary name mismatch).
assert_ok "entrypoint maps AGENT_TOOL=cursor to agent binary" \
    grep -q '\[\[ "${AGENT_TOOL:-}" == "cursor" \]\]' "$ENTRYPOINT"
assert_ok "entrypoint execs agent for cursor fallback" \
    grep -q 'exec agent "$@"' "$ENTRYPOINT"

print_header "=== Entrypoint tool detection: with vertex credentials ==="

VERTEX_CLAUDE_ENV=$(run_detect_with_vertex "claude")
assert_contains "claude+vertex: sets CLAUDE_CODE_USE_VERTEX=1" "$VERTEX_CLAUDE_ENV" "CLAUDE_CODE_USE_VERTEX=1"
assert_contains "claude+vertex: sets DISABLE_AUTOUPDATER=1" "$VERTEX_CLAUDE_ENV" "DISABLE_AUTOUPDATER=1"

VERTEX_OC_ENV=$(run_detect_with_vertex "opencode")
assert_not_contains "opencode+vertex: does NOT set CLAUDE_CODE_USE_VERTEX" "$VERTEX_OC_ENV" "CLAUDE_CODE_USE_VERTEX"
assert_contains "opencode+vertex: sets OPENCODE_DISABLE_AUTOUPDATE=1" "$VERTEX_OC_ENV" "OPENCODE_DISABLE_AUTOUPDATE=1"

echo ""
print_header "=== All test sections complete ==="
