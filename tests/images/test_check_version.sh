#!/bin/bash
# test_check_version.sh -- Test check-agentic-ci-version.sh against a fake podman.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="$SCRIPT_DIR/check-agentic-ci-version.sh"

# shellcheck source=tests/images/shell-utils.sh
source "$SCRIPT_DIR/shell-utils.sh"

PASS=0
FAIL=0
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# A podman that records its arguments and prints FAKE_VERSION_OUTPUT.
FAKE_PODMAN="$TMP_DIR/podman"
cat >"$FAKE_PODMAN" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >"$FAKE_PODMAN_ARGS"
if [[ -n "${FAKE_PODMAN_FAIL:-}" ]]; then exit 125; fi
printf '%s\n' "$FAKE_VERSION_OUTPUT"
EOF
chmod +x "$FAKE_PODMAN"
export FAKE_PODMAN_ARGS="$TMP_DIR/args"

check() {
    PODMAN="$FAKE_PODMAN" FAKE_VERSION_OUTPUT="$1" bash "$CHECK" "$2" "$3" >/dev/null 2>&1
}

expect() {
    local desc="$1" want="$2"; shift 2
    local rc=0
    "$@" || rc=$?
    if [[ "$rc" -eq "$want" ]]; then
        print_success "PASS: $desc"
        PASS=$((PASS + 1))
    else
        print_error "FAIL: $desc (exit $rc, expected $want)"
        FAIL=$((FAIL + 1))
    fi
}

print_header "=== check-agentic-ci-version.sh ==="

expect "release version matches" 0 check "agentic-ci 0.5.0" img:tag 0.5.0
expect "CI build version matches" 0 check "agentic-ci 0.0.0+ci.b17f387" img 0.0.0+ci.b17f387
expect "unknown fallback version fails" 1 check "agentic-ci 0.0.0+unknown" img 0.5.0
expect "other version fails" 1 check "agentic-ci 0.4.0" img 0.5.0
expect "prefix of the expected version fails" 1 check "agentic-ci 0.5" img 0.5.0
check_failing_podman() {
    FAKE_PODMAN_FAIL=1 check "agentic-ci 0.5.0" img 0.5.0
}
expect "podman failure fails" 1 check_failing_podman
expect "missing expected version is a usage error" 2 check "agentic-ci 0.5.0" img ""

check "agentic-ci 0.5.0" quay.io/x/y:ci-1 0.5.0 || true
if [[ "$(cat "$FAKE_PODMAN_ARGS")" == "run --rm --entrypoint agentic-ci quay.io/x/y:ci-1 --version" ]]; then
    print_success "PASS: runs agentic-ci --version in the image"
    PASS=$((PASS + 1))
else
    print_error "FAIL: unexpected podman arguments: $(cat "$FAKE_PODMAN_ARGS")"
    FAIL=$((FAIL + 1))
fi

echo ""
print_success "Passed: $PASS"
if [[ "$FAIL" -gt 0 ]]; then
    print_error "Failed: $FAIL"
    exit 1
fi
