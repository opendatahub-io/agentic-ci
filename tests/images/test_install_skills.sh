#!/bin/bash
# test-install-skills.sh -- Smoke test for images/runner/opencode/install-skills.py
#
# Creates mock git repos (plugin + marketplace), patches the install script
# to clone from local paths instead of GitHub, and verifies the resulting
# skill directory structure for OpenCode.
#
# Requires: python3, git
#
# Usage:
#   ./tests/test-install-skills.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/images/runner/opencode/install-skills.py"

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
---
name: greet
description: Say hello to the user
---
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

OPENCODE_CONFIG_DIR="$TMPDIR_TEST/opencode-config"
export OPENCODE_CONFIG_DIR
SKILLS_DIR="$OPENCODE_CONFIG_DIR/skills"
MKT_CACHE="$TMPDIR_TEST/mkt-cache"

mkdir -p "$MKT_CACHE/marketplaces/$MKT_NAME"
cp "$MOCK_MKT_REPO/.claude-plugin/marketplace.json" \
   "$MKT_CACHE/marketplaces/$MKT_NAME/marketplace.json"

# -- Patch install-skills.py to clone from local mock repo --------------------

PATCHED_SCRIPT="$TMPDIR_TEST/install-skills-patched.py"
sed "s|https://github.com/{repo}.git|$MOCK_PLUGIN_REPO|g" \
    "$INSTALL_SCRIPT" > "$PATCHED_SCRIPT"
chmod +x "$PATCHED_SCRIPT"

# -- Run the patched installer ------------------------------------------------

print_header "=== Running install-skills.py --all ==="

export MARKETPLACE_CACHE_DIR="$MKT_CACHE"

RUN_RC=0
python3 "$PATCHED_SCRIPT" --all >/dev/null 2>&1 || RUN_RC=$?

assert_ok "T-01: install-skills.py exits 0" test "$RUN_RC" -eq 0

# -- Verify results -----------------------------------------------------------

print_header "=== Verifying output structure ==="

assert_ok "T-02: skills directory created" \
    test -d "$SKILLS_DIR"

assert_ok "T-03: greet/SKILL.md exists in skills dir" \
    test -f "$SKILLS_DIR/greet/SKILL.md"

assert_ok "T-04: SKILL.md has correct content" \
    grep -q "Say hello to the user" "$SKILLS_DIR/greet/SKILL.md"

echo ""
print_header "=== All test sections complete ==="
