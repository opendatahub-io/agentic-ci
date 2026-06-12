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
for v in CLAUDE_CODE_USE_VERTEX DISABLE_AUTOUPDATER OPENCODE_DISABLE_AUTOUPDATE; do
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

print_header "=== Entrypoint tool detection: with vertex credentials ==="

VERTEX_CLAUDE_ENV=$(run_detect_with_vertex "claude")
assert_contains "claude+vertex: sets CLAUDE_CODE_USE_VERTEX=1" "$VERTEX_CLAUDE_ENV" "CLAUDE_CODE_USE_VERTEX=1"
assert_contains "claude+vertex: sets DISABLE_AUTOUPDATER=1" "$VERTEX_CLAUDE_ENV" "DISABLE_AUTOUPDATER=1"

VERTEX_OC_ENV=$(run_detect_with_vertex "opencode")
assert_not_contains "opencode+vertex: does NOT set CLAUDE_CODE_USE_VERTEX" "$VERTEX_OC_ENV" "CLAUDE_CODE_USE_VERTEX"
assert_contains "opencode+vertex: sets OPENCODE_DISABLE_AUTOUPDATE=1" "$VERTEX_OC_ENV" "OPENCODE_DISABLE_AUTOUPDATE=1"

print_header "=== Entrypoint: AGENT_ENABLED_PLUGINS enablement ==="

TMPDIR_EP="$(mktemp -d)"
_ep_cleanup() {
    rm -rf "$TMPDIR_EP"
}
trap '_ep_cleanup; print_results' EXIT

EP_HOME="$TMPDIR_EP/claude-home"
mkdir -p "$EP_HOME/plugins"

python3 -c "
import json, sys
data = {
    'version': 2,
    'plugins': {
        'alpha@mkt': [{'scope': 'user', 'installPath': '/tmp/a', 'version': '1.0.0'}],
        'beta@mkt': [{'scope': 'user', 'installPath': '/tmp/b', 'version': '1.0.0'}]
    }
}
with open(sys.argv[1], 'w') as f:
    json.dump(data, f)
" "$EP_HOME/plugins/installed_plugins.json"
echo '{}' > "$EP_HOME/settings.json"

env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_HOME" AGENT_ENABLED_PLUGINS="alpha" \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>/dev/null

ALPHA_ENABLED=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
print('yes' if ep.get('alpha@mkt') else 'no')
" "$EP_HOME/settings.json")
assert_ok "AGENT_ENABLED_PLUGINS=alpha: alpha@mkt enabled" \
    test "$ALPHA_ENABLED" = "yes"

BETA_ENABLED=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
print('yes' if ep.get('beta@mkt') else 'no')
" "$EP_HOME/settings.json")
assert_ok "AGENT_ENABLED_PLUGINS=alpha: beta@mkt NOT enabled" \
    test "$BETA_ENABLED" = "no"

echo '{}' > "$EP_HOME/settings.json"
env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_HOME" AGENT_ENABLED_PLUGINS="alpha,beta" \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>/dev/null

BOTH_ENABLED=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
a = ep.get('alpha@mkt', False)
b = ep.get('beta@mkt', False)
print('yes' if (a and b) else 'no')
" "$EP_HOME/settings.json")
assert_ok "AGENT_ENABLED_PLUGINS=alpha,beta: both enabled" \
    test "$BOTH_ENABLED" = "yes"

python3 -c "
import json, sys
s = {'enabledPlugins': {'alpha@mkt': True, 'beta@mkt': True}}
with open(sys.argv[1], 'w') as f:
    json.dump(s, f)
" "$EP_HOME/settings.json"
env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_HOME" AGENT_ENABLED_PLUGINS="alpha" \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>/dev/null

PREENABLED_ALPHA=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
print('yes' if ep.get('alpha@mkt') else 'no')
" "$EP_HOME/settings.json")
assert_ok "Pre-enabled + AGENT_ENABLED_PLUGINS=alpha: alpha@mkt stays enabled" \
    test "$PREENABLED_ALPHA" = "yes"

PREENABLED_BETA=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
print('yes' if ep.get('beta@mkt') else 'no')
" "$EP_HOME/settings.json")
assert_ok "Pre-enabled + AGENT_ENABLED_PLUGINS=alpha: beta@mkt disabled" \
    test "$PREENABLED_BETA" = "no"

echo '{}' > "$EP_HOME/settings.json"
env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_HOME" \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>/dev/null

ALL_ENABLED=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
a = ep.get('alpha@mkt', False)
b = ep.get('beta@mkt', False)
print('yes' if (a and b) else 'no')
" "$EP_HOME/settings.json")
assert_ok "No AGENT_ENABLED_PLUGINS: all plugins enabled" \
    test "$ALL_ENABLED" = "yes"

echo '{}' > "$EP_HOME/settings.json"
env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_HOME" AGENT_ENABLED_PLUGINS=",,," \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>/dev/null

DEGEN_ALL=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
a = ep.get('alpha@mkt', False)
b = ep.get('beta@mkt', False)
print('yes' if (a and b) else 'no')
" "$EP_HOME/settings.json")
assert_ok "AGENT_ENABLED_PLUGINS=,,,: all plugins enabled (treated as unset)" \
    test "$DEGEN_ALL" = "yes"

EP_NO_CACHE="$TMPDIR_EP/claude-home-no-cache"
mkdir -p "$EP_NO_CACHE"
echo '{}' > "$EP_NO_CACHE/settings.json"
MISSING_CACHE_RC=0
env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_NO_CACHE" AGENT_ENABLED_PLUGINS="alpha" \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>/dev/null || MISSING_CACHE_RC=$?
assert_ok "AGENT_ENABLED_PLUGINS with missing installed_plugins.json: returns 0" \
    test "$MISSING_CACHE_RC" -eq 0

MISSING_CACHE_SETTINGS=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
print('yes' if 'enabledPlugins' not in s else 'no')
" "$EP_NO_CACHE/settings.json")
assert_ok "AGENT_ENABLED_PLUGINS with missing installed_plugins.json: settings.json unchanged" \
    test "$MISSING_CACHE_SETTINGS" = "yes"

echo '{}' > "$EP_HOME/settings.json"
NONEXIST_RC=0
env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_HOME" AGENT_ENABLED_PLUGINS="nonexistent" \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>/dev/null || NONEXIST_RC=$?
assert_ok "AGENT_ENABLED_PLUGINS=nonexistent: exits with error" \
    test "$NONEXIST_RC" -ne 0

echo 'NOT-JSON{{{' > "$EP_HOME/settings.json"
MALFORMED_RC=0
env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_HOME" AGENT_ENABLED_PLUGINS="alpha" \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>/dev/null || MALFORMED_RC=$?
assert_ok "Malformed settings.json: recovers and exits 0" \
    test "$MALFORMED_RC" -eq 0

MALFORMED_ENABLED=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
print('yes' if ep.get('alpha@mkt') else 'no')
" "$EP_HOME/settings.json")
assert_ok "Malformed settings.json: alpha@mkt enabled after recovery" \
    test "$MALFORMED_ENABLED" = "yes"

echo '{}' > "$EP_HOME/settings.json"
MIX_RC=0
MIX_STDERR=$(env -i HOME="$HOME" PATH="$PATH" CLAUDE_CONFIG_DIR="$EP_HOME" AGENT_ENABLED_PLUGINS="alpha,nonexistent" \
    bash -c "source '$ENTRYPOINT' --source-only; _enable_plugins" 2>&1 >/dev/null) || MIX_RC=$?
assert_ok "AGENT_ENABLED_PLUGINS=alpha,nonexistent: exits with error" \
    test "$MIX_RC" -ne 0
assert_contains "AGENT_ENABLED_PLUGINS=alpha,nonexistent: reports matched plugins" \
    "$MIX_STDERR" "Matched: alpha"

echo ""
print_header "=== All test sections complete ==="
