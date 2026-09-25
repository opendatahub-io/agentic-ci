#!/bin/bash
# e2e-openshell-profile.sh -- End-to-end tests for sandbox-profile egress on OpenShell.
#
# Creates a Codex sandbox through OpenShellBackend (tests/e2e/openshell_profile_driver.py)
# first without and then with a sandbox profile that opens the npm and goproxy
# presets, switches the setup shim's egress between the setup, agent and
# validate phases, and probes the network from inside the sandbox after each
# step with python urllib. Never curl: the OpenAI provider binds api.openai.com
# to curl and would confound the probes (RHAI-2936).
#
# No agent runs and no LLM call is made. The OpenAI provider needs a key to be
# created, so a fake one is used and a real OPENAI_API_KEY is never read.
#
# Requires: podman, openshell, openshell-gateway, agentic-ci (the ci-openshell
# image provides all of them), network access to the probed registries.
# Image: CODEX_SANDBOX_IMAGE, or built from the repo when unset.
#
# Destructive: the run deletes the OpenShell sandbox named "ci" and stops the
# gateway, before it starts and again when it exits. Outside CI (CI unset) it
# refuses to start while a "ci" sandbox exists or the gateway is running; set
# E2E_REPLACE_OPENSHELL=1 to let it replace them anyway.
#
# Usage:
#   ./tests/e2e/e2e-openshell-profile.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

source "$SCRIPT_DIR/../images/shell-utils.sh"

PASS=0
FAIL=0
TMPDIR_E2E="$(mktemp -d)"
SANDBOX=ci
SHIM=/usr/local/bin/agentic-ci-sandbox-setup
PROFILE='{"egress": ["npm", "goproxy"]}'
NPM_URL=https://registry.npmjs.org/
GOPROXY_URL=https://proxy.golang.org/

# The provider stores this value; nothing in this script sends it anywhere.
export OPENAI_API_KEY=sk-e2e-openshell-profile-fake-000000000000
# Keep the sandbox identity file inside this run.
export AGENTIC_CI_OPENSHELL_STATE="$TMPDIR_E2E/openshell-sandbox.json"

# Outside CI, refuse to delete a developer's own sandbox or gateway. This runs
# before the cleanup trap is set, because the trap stops them too.
if [[ -z "${CI:-}" && -z "${E2E_REPLACE_OPENSHELL:-}" ]]; then
    # The same test gateway.is_running() applies.
    gateway_status="$(openshell status 2>/dev/null)" && gateway_up=1 || gateway_up=""
    if [[ -n "$gateway_up" ]] && grep -q "No gateway configured" <<<"$gateway_status"; then
        gateway_up=""
    fi
    if [[ -n "$gateway_up" ]] || openshell sandbox get "$SANDBOX" >/dev/null 2>&1; then
        print_error "An OpenShell gateway or \"$SANDBOX\" sandbox exists and this run would delete it. Stop it first or set E2E_REPLACE_OPENSHELL=1."
        rm -rf "$TMPDIR_E2E"
        exit 1
    fi
fi

cleanup() {
    local rc=$?
    agentic-ci stop --backend openshell --harness codex >/dev/null 2>&1 || true
    rm -rf "$TMPDIR_E2E"
    echo ""
    print_header "=== Results ==="
    print_success "Passed: $PASS"
    if [[ "$FAIL" -gt 0 ]]; then
        print_error "Failed: $FAIL"
        exit 1
    elif [[ "$rc" -ne 0 ]]; then
        print_error "Aborted with exit status $rc before all checks ran"
        exit "$rc"
    else
        print_success "All tests passed!"
    fi
}
trap cleanup EXIT

pass() { print_success "PASS: $1"; PASS=$((PASS + 1)); }
fail() { print_error "FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_ok() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}

# expect RESULT DESC -- CMD...: run CMD in the sandbox and compare the probe's
# verdict (ALLOWED, DENIED or ERROR) with RESULT.
expect() {
    local want="$1" desc="$2"; shift 3
    local out
    out="$(timeout 90 openshell sandbox exec --name "$SANDBOX" --no-tty -- "$@" 2>&1 || true)"
    if grep -q "^PROBE $want" <<<"$out"; then
        pass "$desc ($want)"
    else
        fail "$desc: expected $want"
        echo "  Got: $(tr '\n' ' ' <<<"$out" | cut -c1-240)"
    fi
}

driver() {
    "$(agentic_python)" "$SCRIPT_DIR/openshell_profile_driver.py" "$@" \
        --image "$CODEX_SANDBOX" --workdir "$WORKDIR"
}

policy_snapshot() {
    openshell policy get --base -o json "$SANDBOX" > "$TMPDIR_E2E/policy-$1.json"
}

# --- Resolve sandbox image and supervisor ---
if [[ -n "${CODEX_SANDBOX_IMAGE:-}" ]]; then
    CODEX_SANDBOX="$CODEX_SANDBOX_IMAGE"
else
    print_step "Building codex-sandbox..."
    podman build -t localhost/codex-sandbox:latest \
        -f "$REPO_ROOT/images/runner/codex/Containerfile.openshell" "$REPO_ROOT"
    CODEX_SANDBOX="localhost/codex-sandbox:latest"
fi
print_step "codex-sandbox: $CODEX_SANDBOX"

if [[ -n "${SUPERVISOR_IMAGE:-}" ]]; then
    export OPENSHELL_SUPERVISOR_IMAGE="$SUPERVISOR_IMAGE"
elif [[ -z "${OPENSHELL_SUPERVISOR_IMAGE:-}" ]]; then
    os_tag="$(grep -oP 'ARG OPENSHELL_IMAGE_TAG=\K\S+' \
        "$REPO_ROOT/images/ci/Containerfile.openshell" 2>/dev/null || true)"
    export OPENSHELL_SUPERVISOR_IMAGE="quay.io/opendatahub/odh-openshell-supervisor:${os_tag:-latest}"
fi
print_step "supervisor: $OPENSHELL_SUPERVISOR_IMAGE"
print_step "openshell: $(openshell --version 2>&1 || echo unknown)"

# --- Workdir with the probe ---
# The backend uploads the workdir to /sandbox/<name>, so the probe is there.
WORKDIR="$TMPDIR_E2E/profile-e2e"
mkdir -p "$WORKDIR"
cat > "$WORKDIR/probe.py" <<'PROBE'
"""Fetch a URL through the sandbox proxy and print one PROBE line."""

import sys
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=20) as response:
        print(f"PROBE ALLOWED http={response.status}")
except urllib.error.HTTPError as exc:
    # The server answered, so the connection was allowed.
    print(f"PROBE ALLOWED http={exc.code}")
except OSError as exc:
    if "Tunnel connection failed: 403" in str(exc):
        print("PROBE DENIED")
    else:
        print(f"PROBE ERROR {type(exc).__name__}: {exc}")
PROBE
PROBE="/sandbox/$(basename "$WORKDIR")/probe.py"
PY_PROBE=(python3 "$PROBE")
CODEX_EXEC=(codex sandbox -c 'sandbox_mode="danger-full-access"' --)
GITHUB_URL=https://github.com/

# --- Start from a clean gateway and sandbox ---
# setup() cannot reuse a "ci" sandbox this run did not record in its fresh
# identity file, so an earlier run's leftover (e2e-openshell-sandbox.sh stops
# with "|| true") would fail it.
agentic-ci stop --backend openshell --harness codex >/dev/null 2>&1 || true

# ============================================================================
print_header "=== 1. No profile: presets are closed ==="
NOPROFILE_LOG="$TMPDIR_E2E/setup-noprofile.log"
if driver setup > "$NOPROFILE_LOG" 2>&1; then
    pass "sandbox created without a profile"
else
    fail "sandbox created without a profile"
    cat "$NOPROFILE_LOG"
    exit 1
fi
assert_ok "no profile: policy source is the built-in default" \
    grep -q "Policy source: built-in default" "$NOPROFILE_LOG"
assert_ok "no profile: identity has no profile hash" \
    python3 -c "import json,sys; sys.exit('profile_hash' in json.load(open(sys.argv[1])))" \
    "$AGENTIC_CI_OPENSHELL_STATE"
assert_ok "setup shim is in the sandbox image" \
    openshell sandbox exec --name "$SANDBOX" --no-tty -- test -x "$SHIM"

expect DENIED "no profile: npm from bare exec" -- "${PY_PROBE[@]}" "$NPM_URL"
expect DENIED "no profile: goproxy from bare exec" -- "${PY_PROBE[@]}" "$GOPROXY_URL"
expect DENIED "no profile: npm from the shim" -- "$SHIM" "${PY_PROBE[@]}" "$NPM_URL"
expect DENIED "no profile: npm from codex" -- "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" "$NPM_URL"

# ============================================================================
print_header "=== 2. Profile (npm, goproxy): setup phase ==="
PROFILE_LOG="$TMPDIR_E2E/setup-profile.log"
if driver setup --profile-json "$PROFILE" > "$PROFILE_LOG" 2>&1; then
    pass "sandbox recreated with a profile"
else
    fail "sandbox recreated with a profile"
    cat "$PROFILE_LOG"
    exit 1
fi
assert_ok "profile: a new profile recreates the sandbox" \
    grep -q "Sandbox identity changed" "$PROFILE_LOG"
assert_ok "profile: policy source is the sandbox profile" \
    grep -q "Policy source: sandbox profile" "$PROFILE_LOG"
assert_ok "profile: identity records the profile hash" \
    python3 -c "import json,sys; sys.exit('profile_hash' not in json.load(open(sys.argv[1])))" \
    "$AGENTIC_CI_OPENSHELL_STATE"
policy_snapshot 0-create

assert_ok "switch to the setup phase" driver phase setup --profile-json "$PROFILE"
policy_snapshot 1-setup

expect ALLOWED "setup: npm from shim -> python" -- "$SHIM" "${PY_PROBE[@]}" "$NPM_URL"
expect ALLOWED "setup: npm from shim -> bash -> python" -- \
    "$SHIM" bash -c 'python3 "$0" "$1"' "$PROBE" "$NPM_URL"
expect ALLOWED "setup: goproxy from shim -> python" -- "$SHIM" "${PY_PROBE[@]}" "$GOPROXY_URL"
expect DENIED "setup: npm from bare bash -> python" -- \
    bash -c 'python3 "$0" "$1"' "$PROBE" "$NPM_URL"
expect DENIED "setup: npm from bare python" -- "${PY_PROBE[@]}" "$NPM_URL"
expect DENIED "setup: example.com from the shim" -- "$SHIM" "${PY_PROBE[@]}" https://example.com/
expect DENIED "setup: dl.google.com from the shim" -- \
    "$SHIM" "${PY_PROBE[@]}" https://dl.google.com/
expect DENIED "setup: api.openai.com from the shim via python" -- \
    "$SHIM" "${PY_PROBE[@]}" https://api.openai.com/v1/models
# The agent's rules are parked while the shim's egress is open, so running an
# agent binary under the shim gains no agent egress.
expect DENIED "setup: github.com from shim -> codex -> python" -- \
    "$SHIM" "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" "$GITHUB_URL"
expect DENIED "setup: api.openai.com from shim -> codex -> python" -- \
    "$SHIM" "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" https://api.openai.com/v1/models
expect DENIED "setup: github.com from codex -> python" -- \
    "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" "$GITHUB_URL"

# A process the shim leaves behind (setsid double fork) is reparented away
# from the shim, so it loses the shim's egress once the shim has exited.
ORPHAN_OUT=/tmp/agentic-ci-e2e-orphan.out
openshell sandbox exec --name "$SANDBOX" --no-tty -- rm -f "$ORPHAN_OUT" >/dev/null 2>&1 || true
openshell sandbox exec --name "$SANDBOX" --no-tty -- "$SHIM" bash -c \
    'setsid -f sh -c "sleep 3; python3 \"\$0\" \"\$1\" > \"\$2\" 2>&1" "$0" "$1" "$2"' \
    "$PROBE" "$NPM_URL" "$ORPHAN_OUT" >/dev/null 2>&1 || true
ORPHAN_RESULT=""
for _ in $(seq 1 20); do
    sleep 2
    ORPHAN_RESULT="$(openshell sandbox exec --name "$SANDBOX" --no-tty -- \
        cat "$ORPHAN_OUT" 2>/dev/null || true)"
    grep -q "^PROBE" <<<"$ORPHAN_RESULT" && break
done
if grep -q "^PROBE DENIED" <<<"$ORPHAN_RESULT"; then
    pass "setup: npm from a process orphaned after the shim exited (DENIED)"
else
    fail "setup: npm from a process orphaned after the shim exited: expected DENIED"
    echo "  Got: ${ORPHAN_RESULT:0:240}"
fi

# ============================================================================
print_header "=== 3. Agent phase ==="
assert_ok "switch to the agent phase" driver phase agent --profile-json "$PROFILE"
policy_snapshot 2-agent

expect DENIED "agent: npm from the shim" -- "$SHIM" "${PY_PROBE[@]}" "$NPM_URL"
expect DENIED "agent: goproxy from the shim" -- "$SHIM" "${PY_PROBE[@]}" "$GOPROXY_URL"
expect ALLOWED "agent: npm from codex (agent-phase preset)" -- \
    "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" "$NPM_URL"
expect ALLOWED "agent: github.com from codex (agent rules restored)" -- \
    "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" "$GITHUB_URL"
expect DENIED "agent: example.com from codex" -- \
    "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" https://example.com/

# ============================================================================
print_header "=== 4. Validate phase, then agent again ==="
assert_ok "switch to the validate phase" driver phase validate --profile-json "$PROFILE"
policy_snapshot 3-validate

expect ALLOWED "validate: npm from the shim" -- "$SHIM" "${PY_PROBE[@]}" "$NPM_URL"
expect DENIED "validate: npm from bare python" -- "${PY_PROBE[@]}" "$NPM_URL"
expect DENIED "validate: github.com from shim -> codex -> python" -- \
    "$SHIM" "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" "$GITHUB_URL"

assert_ok "switch back to the agent phase" driver phase agent --profile-json "$PROFILE"
policy_snapshot 4-agent

expect DENIED "agent again: npm from the shim" -- "$SHIM" "${PY_PROBE[@]}" "$NPM_URL"
expect ALLOWED "agent again: github.com from codex" -- \
    "${CODEX_EXEC[@]}" "${PY_PROBE[@]}" "$GITHUB_URL"

# Reusing the sandbox (same profile) after a run stopped in the setup phase
# must put the agent phase back before anything else runs.
assert_ok "switch to the setup phase before a reuse" \
    driver phase setup --profile-json "$PROFILE"
REUSE_LOG="$TMPDIR_E2E/setup-reuse.log"
if driver setup --profile-json "$PROFILE" > "$REUSE_LOG" 2>&1; then
    pass "sandbox reused with the same profile"
else
    fail "sandbox reused with the same profile"
    cat "$REUSE_LOG"
fi
assert_ok "reuse: the existing sandbox is kept" grep -q "Sandbox already exists" "$REUSE_LOG"
policy_snapshot 5-reuse
expect DENIED "reuse: npm from the shim" -- "$SHIM" "${PY_PROBE[@]}" "$NPM_URL"

# ============================================================================
print_header "=== 5. Policy structure across switches ==="
POLICY_CHECK="$(python3 - "$TMPDIR_E2E" "$SHIM" /proc/agentic-ci-parked 2>&1 <<'CHECK'
import json
import sys
from pathlib import Path

run_dir, shim, parked = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
names = ["0-create", "1-setup", "2-agent", "3-validate", "4-agent", "5-reuse"]
agent_names = ["0-create", "2-agent", "4-agent", "5-reuse"]
shim_names = ["1-setup", "3-validate"]
policies = {n: json.loads((run_dir / f"policy-{n}.json").read_text())["policy"] for n in names}
static = {n: {k: v for k, v in p.items() if k != "network_policies"} for n, p in policies.items()}
rules = {n: p["network_policies"] for n, p in policies.items()}


def phase_rules(name):
    return {k: v for k, v in rules[name].items() if k.startswith("agentic_ci_phase_")}


def shim_bound(name):
    return [k for k, v in rules[name].items() if any(b.get("path") == shim for b in v["binaries"])]


def unparked(rule):
    paths = [b["path"].removeprefix(parked) for b in rule["binaries"]]
    return {**rule, "binaries": [{"path": p} for p in paths]}


def agent_rules(name):
    return {k: v for k, v in rules[name].items() if not k.startswith("agentic_ci_phase_")}


checks = {
    "static_fields_unchanged": all(static[n] == static["0-create"] for n in names),
    "filesystem_policy_present": "filesystem_policy" in static["0-create"],
    "agent_restores_create_rules": all(rules[n] == rules["0-create"] for n in agent_names),
    "no_shim_rule_at_create": not shim_bound("0-create"),
    "no_parked_binary_in_agent_phase": all(
        not b["path"].startswith(parked)
        for n in agent_names
        for v in rules[n].values()
        for b in v["binaries"]
    ),
    "no_empty_binaries": all(v["binaries"] for n in names for v in rules[n].values()),
}
for n in shim_names:
    checks[f"{n}_rules_shim_only"] = bool(phase_rules(n)) and all(
        v["binaries"] == [{"path": shim}] for v in phase_rules(n).values()
    )
    checks[f"{n}_shim_only_in_phase_rules"] = set(shim_bound(n)) == set(phase_rules(n))
    checks[f"{n}_agent_rules_all_parked"] = all(
        b["path"].startswith(parked + "/") for v in agent_rules(n).values() for b in v["binaries"]
    )
    checks[f"{n}_agent_rules_otherwise_unchanged"] = {
        k: unparked(v) for k, v in agent_rules(n).items()
    } == rules["0-create"]
for name, ok in checks.items():
    print(f"POLICY_CHECK {name}={'ok' if ok else 'fail'}")
CHECK
)" || true
if grep -q "^POLICY_CHECK " <<<"$POLICY_CHECK"; then
    while read -r line; do
        name="${line#POLICY_CHECK }"
        if [[ "$name" == *=ok ]]; then pass "policy: ${name%=ok}"; else fail "policy: $name"; fi
    done < <(grep "^POLICY_CHECK " <<<"$POLICY_CHECK")
else
    fail "policy: structure checks did not run"
    echo "  Got: ${POLICY_CHECK:0:400}"
fi

echo ""
print_header "=== All test sections complete ==="
