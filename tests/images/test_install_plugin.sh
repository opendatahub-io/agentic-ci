#!/bin/bash
# test-install-plugin.sh -- Smoke test for image/install-plugin.py
#
# Creates mock git repos (plugin + marketplace), patches the install script
# to clone from local paths instead of GitHub, and verifies the resulting
# cache directory structure and metadata files.
#
# Requires: python3, git
#
# Usage:
#   ./tests/test-install-plugin.sh

set -euo pipefail

# shellcheck source=shell-utils.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/images/runner/claude-code/install-plugin.py"

source "$SCRIPT_DIR/shell-utils.sh"

PASS=0
FAIL=0
TMPDIR_TEST="$(mktemp -d)"

cleanup() {
    rm -rf "$TMPDIR_TEST"
    echo ""
    print_header "=== Results ==="
    print_success "Passed: $PASS"
    if [[ "$FAIL" -gt 0 ]]; then
        print_error "Failed: $FAIL"
        exit 1
    else
        print_success "All tests passed!"
        exit 0
    fi
}
trap cleanup EXIT

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

# -- Preflight ---------------------------------------------------------------

print_header "=== Preflight checks ==="
check_dependencies python3 git

# -- Build mock plugin repo ---------------------------------------------------

print_header "=== Setting up mock repos ==="

MOCK_PLUGIN_REPO="$TMPDIR_TEST/mock-plugin-repo"
mkdir -p "$MOCK_PLUGIN_REPO/.claude/skills/greet"
cat > "$MOCK_PLUGIN_REPO/.claude/skills/greet/SKILL.md" << 'EOF'
# Greet skill
Say hello to the user.
EOF

git -C "$MOCK_PLUGIN_REPO" init --quiet -b main
git -C "$MOCK_PLUGIN_REPO" add -A
git -C "$MOCK_PLUGIN_REPO" \
    -c user.email="test@test" -c user.name="test" \
    commit --quiet -m "init"

# -- Build mock marketplace repo ----------------------------------------------

MOCK_MKT_REPO="$TMPDIR_TEST/mock-mkt-repo"
MKT_NAME="test-marketplace"
PLUGIN_NAME="mock-greet"
PLUGIN_VERSION="1.0.0"

mkdir -p "$MOCK_MKT_REPO/.claude-plugin"
cat > "$MOCK_MKT_REPO/.claude-plugin/marketplace.json" << MKEOF
{
  "name": "$MKT_NAME",
  "plugins": [
    {
      "name": "$PLUGIN_NAME",
      "version": "$PLUGIN_VERSION",
      "source": {
        "repo": "fake-org/mock-greet",
        "ref": "main"
      }
    }
  ]
}
MKEOF

git -C "$MOCK_MKT_REPO" init --quiet -b main
git -C "$MOCK_MKT_REPO" add -A
git -C "$MOCK_MKT_REPO" \
    -c user.email="test@test" -c user.name="test" \
    commit --quiet -m "init"

# -- Pre-register the marketplace (bypass --marketplace-repo) -----------------

CLAUDE_HOME="$TMPDIR_TEST/claude-home"
export CLAUDE_HOME
PLUGINS_DIR="$CLAUDE_HOME/plugins"

mkdir -p "$PLUGINS_DIR/marketplaces/$MKT_NAME"
cp "$MOCK_MKT_REPO/.claude-plugin/marketplace.json" \
   "$PLUGINS_DIR/marketplaces/$MKT_NAME/marketplace.json"

# -- Patch install-plugin.py to clone from local mock repo --------------------

PATCHED_SCRIPT="$TMPDIR_TEST/install-plugin-patched.py"
sed "s|https://github.com/{repo}.git|$MOCK_PLUGIN_REPO|g" \
    "$INSTALL_SCRIPT" > "$PATCHED_SCRIPT"
chmod +x "$PATCHED_SCRIPT"

# -- Run the patched installer ------------------------------------------------

print_header "=== Running install-plugin.py --all ==="

RUN_RC=0
python3 "$PATCHED_SCRIPT" --all >/dev/null 2>&1 || RUN_RC=$?

assert_ok "T-01: install-plugin.py exits 0" test "$RUN_RC" -eq 0

# -- Verify results -----------------------------------------------------------

print_header "=== Verifying output structure ==="

CACHE_DEST="$PLUGINS_DIR/cache/$MKT_NAME/$PLUGIN_NAME/$PLUGIN_VERSION"

# Cache directory exists
assert_ok "T-02: cache directory created" \
    test -d "$CACHE_DEST"

# Skill file copied
assert_ok "T-03: skills/greet/SKILL.md exists in cache" \
    test -f "$CACHE_DEST/skills/greet/SKILL.md"

# .in_use marker
assert_ok "T-04: .in_use marker exists" \
    test -f "$CACHE_DEST/.in_use"

# installed_plugins.json has the plugin entry
INSTALLED_JSON="$PLUGINS_DIR/installed_plugins.json"
assert_ok "T-05: installed_plugins.json exists" \
    test -f "$INSTALLED_JSON"

PLUGIN_KEY="${PLUGIN_NAME}@${MKT_NAME}"
HAS_ENTRY=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print('yes' if sys.argv[2] in d.get('plugins', {}) else 'no')
" "$INSTALLED_JSON" "$PLUGIN_KEY")
assert_ok "T-06: installed_plugins.json has '$PLUGIN_KEY'" \
    test "$HAS_ENTRY" = "yes"

# settings.json has enabledPlugins entry
SETTINGS_JSON="$CLAUDE_HOME/settings.json"
assert_ok "T-07: settings.json exists" \
    test -f "$SETTINGS_JSON"

HAS_ENABLED=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
print('yes' if sys.argv[2] in ep else 'no')
" "$SETTINGS_JSON" "$PLUGIN_KEY")
assert_ok "T-08: settings.json enabledPlugins has '$PLUGIN_KEY'" \
    test "$HAS_ENABLED" = "yes"

# -- Test --no-enable flag ----------------------------------------------------

print_header "=== Testing --no-enable flag ==="

NOENABLE_HOME="$TMPDIR_TEST/claude-home-noenable"
NOENABLE_PLUGINS="$NOENABLE_HOME/plugins"
mkdir -p "$NOENABLE_PLUGINS/marketplaces/$MKT_NAME"
cp "$MOCK_MKT_REPO/.claude-plugin/marketplace.json" \
   "$NOENABLE_PLUGINS/marketplaces/$MKT_NAME/marketplace.json"

PATCHED_NOENABLE="$TMPDIR_TEST/install-plugin-noenable.py"
sed "s|https://github.com/{repo}.git|$MOCK_PLUGIN_REPO|g" \
    "$INSTALL_SCRIPT" > "$PATCHED_NOENABLE"
chmod +x "$PATCHED_NOENABLE"

NOENABLE_RC=0
CLAUDE_HOME="$NOENABLE_HOME" python3 "$PATCHED_NOENABLE" --all --no-enable \
    >/dev/null 2>&1 || NOENABLE_RC=$?

assert_ok "T-09: --no-enable exits 0" test "$NOENABLE_RC" -eq 0

NOENABLE_CACHE="$NOENABLE_PLUGINS/cache/$MKT_NAME/$PLUGIN_NAME/$PLUGIN_VERSION"
assert_ok "T-10: --no-enable cache directory created" \
    test -d "$NOENABLE_CACHE"

assert_ok "T-11: --no-enable skills cached" \
    test -f "$NOENABLE_CACHE/skills/greet/SKILL.md"

NOENABLE_INSTALLED="$NOENABLE_PLUGINS/installed_plugins.json"
NOENABLE_HAS_ENTRY=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print('yes' if sys.argv[2] in d.get('plugins', {}) else 'no')
" "$NOENABLE_INSTALLED" "$PLUGIN_KEY")
assert_ok "T-12: --no-enable installed_plugins.json has entry" \
    test "$NOENABLE_HAS_ENTRY" = "yes"

NOENABLE_SETTINGS="$NOENABLE_HOME/settings.json"
if [[ -f "$NOENABLE_SETTINGS" ]]; then
    NOENABLE_HAS_ENABLED=$(python3 -c "
import json, sys
s = json.load(open(sys.argv[1]))
ep = s.get('enabledPlugins', {})
print('yes' if sys.argv[2] in ep else 'no')
" "$NOENABLE_SETTINGS" "$PLUGIN_KEY")
else
    NOENABLE_HAS_ENABLED="no"
fi
assert_ok "T-13: --no-enable settings.json does NOT have enabledPlugins entry" \
    test "$NOENABLE_HAS_ENABLED" = "no"

echo ""
print_header "=== All test sections complete ==="
