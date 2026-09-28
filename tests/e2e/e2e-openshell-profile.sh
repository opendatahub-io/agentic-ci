#!/bin/bash
# e2e-openshell-profile.sh -- End-to-end tests for sandbox-profile egress on OpenShell.
#
# Creates a Codex sandbox through OpenShellBackend (tests/e2e/openshell_profile_driver.py)
# first without and then with a sandbox profile that opens the npm, goproxy and
# pypi presets (plus two raw endpoints for preset hosts, which must give way to
# the presets in every phase), switches the setup shim's egress between the setup, agent and
# validate phases, and probes the network from inside the sandbox after each
# step with python urllib. Section 6 checks that the presets are read-only at
# L7: GETs (and the Go proxy's redirect to Cloud Storage) pass, PUT and POST
# get the proxy's own 403, and npm and uv work through the shim.
#
# A refused connection looks different across OpenShell releases: v0.0.116
# answers the CONNECT with 403 ("Tunnel connection failed: 403", curl's
# "CONNECT tunnel failed, response 403"), v0.1.x refuses the socket connect
# ("[Errno 13] Permission denied", curl's "(7) Failed to connect"). Either
# counts as a denial only together with the sandbox log's DENIED line for
# that caller and host, stamped after the check started (`openshell logs`):
# a bare EACCES, connect failure or 403 proves nothing.
#
# The shim is an extra layer, not isolation, so sections 7 to 9 check what
# keeps the agent out of the setup and validate phases: processes the agent
# left running (a nohup job, setsid daemons, one named like the sandbox's main
# process, one that keeps trying the shim, and a planted ~/.bash_profile that
# would forge the switch's own execs under a login shell) are gone once
# validate opens, while the main process and the sandbox stay usable; and the
# OpenAI provider is detached, so curl is refused instead of getting an
# injected key, even from under codex or with a placeholder issued in the
# agent phase, until the agent phase attaches it again. A control attaches the
# provider by hand in the setup phase to show that codex -> curl is injected
# then. Section 9 repeats those provider checks for api-key auth: a Claude
# Code sandbox (the harness and auth mode change recreates it) with the
# agentic-ci-anthropic provider, ANTHROPIC_API_KEY and the claude binary.
# Only the provider checks use curl.
#
# No agent runs and no LLM call is made. The API key providers need a key to
# be created, so fake ones are used and a real OPENAI_API_KEY or
# ANTHROPIC_API_KEY is never read. The key checks send the fake key to the
# API: OpenAI's 401 echoes it masked ("Incorrect API key provided:
# sk-e2e-o***"), and Anthropic answers a key-shaped value with "API key is
# invalid." but an unresolved placeholder with "invalid x-api-key", which
# shows the proxy injected it. Section 9 runs commands under the claude binary
# from a SessionStart hook that stops claude before it sends its prompt.
#
# Requires: podman, openshell, openshell-gateway, agentic-ci (the ci-openshell
# image provides all of them), network access to the probed registries.
# Images: CODEX_SANDBOX_IMAGE and CLAUDE_SANDBOX_IMAGE, each built from the
# repo when unset.
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
# Host-side filter for the sandbox log (see denial_logged).
DENIALS="$TMPDIR_E2E/denials.py"
SANDBOX=ci
SHIM=/usr/local/bin/agentic-ci-sandbox-setup
# The raw endpoints overlap preset hosts (registry.npmjs.org exactly,
# storage.googleapis.com through a wildcard) with write access; the presets
# replace them, so they must not reopen writes in any phase.
PROFILE='{"egress": ["npm", "goproxy", "pypi", "registry.npmjs.org:443:full", "*.googleapis.com:443:read-write"]}'
NPM_URL=https://registry.npmjs.org/
GOPROXY_URL=https://proxy.golang.org/

# The providers store these values; only the key checks send them, to the
# API they are for. Section 9 exports ANTHROPIC_API_KEY, which selects
# api-key auth for the Claude Code harness.
export OPENAI_API_KEY=sk-e2e-openshell-profile-fake-000000000000
FAKE_ANTHROPIC_API_KEY=sk-ant-e2e-openshell-profile-fake-000000000000
unset ANTHROPIC_API_KEY
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

# The executable the sandbox log names for the probe (the python3 symlink
# resolved, such as /usr/bin/python3.14) and for curl.
PY_CALLER='/usr/bin/python3(\.[0-9]+)?'
CURL_CALLER=/usr/bin/curl

# read_log: the sandbox log of the last 10 minutes, empty when it cannot be read.
read_log() { openshell logs "$SANDBOX" --source sandbox --since 10m -n 5000 2>/dev/null || true; }

# denial_mark CALLER_RE HOST [METHOD]: the MARK for denial_logged, taken right
# before the probe runs: the later of now and the stamp of the newest sandbox
# log line that already records such a denial. The sandbox shares the host's
# clock (same kernel), so a line an earlier probe caused is stamped before
# now; the newest-line stamp keeps it out even if the two clocks drift apart.
denial_mark() { read_log | python3 "$DENIALS" --mark "$@"; }

# denial_logged MARK CALLER_RE HOST [METHOD]: print the first sandbox log line
# stamped strictly after MARK (see denial_mark) that records a denial: without
# METHOD a refused connection from CALLER_RE to HOST ("DENIED
# /usr/bin/curl(0) -> HOST:443"), with METHOD an L7 denial of METHOD to HOST
# ("DENIED PUT http://HOST:443/..."; these lines name no caller). Polls the
# log for 20 s at most, since the supervisor ships it with a delay; returns
# non-zero when no such line shows up.
denial_logged() {
    local mark="$1"; shift
    for _ in $(seq 20); do
        if read_log | python3 "$DENIALS" "$mark" "$@"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# probe_host CMD...: the host of the first https:// argument.
probe_host() {
    local arg host
    for arg in "$@"; do
        if [[ "$arg" == https://* ]]; then
            host="${arg#https://}"
            echo "${host%%/*}"
            return
        fi
    done
}

# probe_method CMD...: the probe's METHOD argument (the one after the URL), or GET.
probe_method() {
    while (($#)); do
        if [[ "$1" == https://* ]]; then
            echo "${2:-GET}"
            return
        fi
        shift
    done
    echo GET
}

# expect RESULT DESC -- CMD...: run CMD in the sandbox and compare the probe's
# verdict (ALLOWED, DENIED, L7DENIED or ERROR) with RESULT. DENIED is a
# refused connection: the probe's BLOCKED verdict (proxy-403 or eacces, see
# the top of this file) with the sandbox log's DENIED line for python and
# the URL's host. L7DENIED is the proxy's own 403 policy_denied answer after
# it inspected the request, with the log's DENIED line for the method and
# host.
expect() {
    local want="$1" desc="$2"; shift 3
    local out mark="" host method shape=""
    host="$(probe_host "$@")"
    method="$(probe_method "$@")"
    case "$want" in
        DENIED) mark="$(denial_mark "$PY_CALLER" "$host")" ;;
        L7DENIED) mark="$(denial_mark "" "$host" "$method")" ;;
    esac
    out="$(timeout 90 openshell sandbox exec --name "$SANDBOX" --no-tty -- "$@" 2>&1 || true)"
    case "$want" in
        DENIED)
            shape="$(sed -n 's/^PROBE BLOCKED \([a-z0-9-]*\).*/\1/p' <<<"$out" | head -1)"
            if [[ -n "$shape" ]] && denial_logged "$mark" "$PY_CALLER" "$host" >/dev/null; then
                pass "$desc (DENIED: $shape, logged)"
                return
            fi
            ;;
        L7DENIED)
            grep -q "^PROBE L7DENIED" <<<"$out" && shape=policy_denied
            if [[ -n "$shape" ]] && denial_logged "$mark" "" "$host" "$method" >/dev/null; then
                pass "$desc (L7DENIED, logged)"
                return
            fi
            ;;
        *)
            if grep -q "^PROBE $want" <<<"$out"; then
                pass "$desc ($want)"
                return
            fi
            ;;
    esac
    fail "$desc: expected $want"
    echo "  Got: $(tr '\n' ' ' <<<"$out" | cut -c1-240)"
    if [[ -n "$shape" ]]; then
        echo "  Sandbox log: no matching DENIED line for $host after $mark"
    fi
}

# driver ARGS...: run the driver for HARNESS in HARNESS_IMAGE (codex until
# section 9 switches to claude-code).
driver() {
    "$(agentic_python)" "$SCRIPT_DIR/openshell_profile_driver.py" "$@" \
        --harness "$HARNESS" --image "$HARNESS_IMAGE" --workdir "$WORKDIR"
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
if [[ -n "${CLAUDE_SANDBOX_IMAGE:-}" ]]; then
    CLAUDE_SANDBOX="$CLAUDE_SANDBOX_IMAGE"
else
    print_step "Building claude-sandbox..."
    podman build -t localhost/claude-sandbox:latest \
        -f "$REPO_ROOT/images/runner/claude-code/Containerfile.openshell" "$REPO_ROOT"
    CLAUDE_SANDBOX="localhost/claude-sandbox:latest"
fi
print_step "claude-sandbox: $CLAUDE_SANDBOX"
HARNESS=codex
HARNESS_IMAGE="$CODEX_SANDBOX"

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
"""Send one request through the sandbox proxy and print one PROBE line.

Usage: probe.py URL [METHOD]. A GET asks for the first KiB only; any other
method sends a small body. The verdict is ALLOWED (the server answered, with
its status and the host of the final URL after redirects), BLOCKED proxy-403
(the proxy refused the CONNECT, OpenShell v0.0.116), BLOCKED eacces (the
connect failed with EACCES, OpenShell v0.1.x), L7DENIED (the proxy answered
403 policy_denied itself after inspecting the request) or ERROR. A BLOCKED
verdict is a denial only with the sandbox log's DENIED line for it, which
the caller checks.
"""

import errno
import sys
import urllib.error
import urllib.parse
import urllib.request

url = sys.argv[1]
method = sys.argv[2] if len(sys.argv) > 2 else "GET"
if method == "GET":
    request = urllib.request.Request(url, headers={"Range": "bytes=0-1023"})
else:
    request = urllib.request.Request(
        url, data=b"agentic-ci e2e", method=method, headers={"Content-Type": "text/plain"}
    )
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        host = urllib.parse.urlsplit(response.url).hostname
        print(f"PROBE ALLOWED http={response.status} host={host}")
except urllib.error.HTTPError as exc:
    body = exc.read(4096).decode("utf-8", "replace")
    if exc.code == 403 and "policy_denied" in body:
        print("PROBE L7DENIED")
    else:
        # The server answered, so the request was allowed.
        print(f"PROBE ALLOWED http={exc.code} host={urllib.parse.urlsplit(exc.url).hostname}")
except OSError as exc:
    reason = getattr(exc, "reason", exc)
    if "Tunnel connection failed: 403" in str(exc):
        print("PROBE BLOCKED proxy-403")
    elif isinstance(reason, OSError) and reason.errno == errno.EACCES:
        print("PROBE BLOCKED eacces")
    else:
        print(f"PROBE ERROR {type(exc).__name__}: {exc}")
PROBE
cat > "$DENIALS" <<'DENIALS_PY'
"""Find a sandbox log line on stdin that records a denial.

Usage: denials.py MARK CALLER_RE HOST [METHOD] prints the first matching
line stamped strictly after MARK (epoch seconds) and exits 0, else exits 1.
denials.py --mark CALLER_RE HOST [METHOD] prints the later of now and the
stamp of the newest matching line, the MARK for a probe about to run.

Without METHOD a line matches when it records a refused connection from an
executable matching CALLER_RE to HOST, as OpenShell v0.0.116 and v0.1.x both
log it:

    [1790609532.256] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED
        /usr/bin/curl(0) -> api.openai.com:443 [reason:...]

With METHOD it matches an L7 denial of METHOD to HOST ("DENIED PUT
http://HOST:443/path", the port optional).
"""

import re
import sys
import time

args = sys.argv[1:]
mark_mode = args[0] == "--mark"
if not mark_mode:
    after = float(args.pop(0))
else:
    args.pop(0)
caller, host = args[0], re.escape(args[1])
method = args[2] if len(args) > 2 else ""
if method:
    pattern = re.compile(rf"\bDENIED {re.escape(method)} https?://{host}(?::[0-9]+)?/")
else:
    pattern = re.compile(rf"\bDENIED (?:{caller})\([0-9]+\) -> {host}:[0-9]+(?![0-9])")
stamp = re.compile(r"^\[([0-9]+(?:\.[0-9]+)?)\]")
newest = time.time()
for line in sys.stdin:
    match = stamp.match(line)
    if not match or not pattern.search(line):
        continue
    stamped = float(match.group(1))
    if mark_mode:
        newest = max(newest, stamped)
    elif stamped > after:
        print(line.strip())
        sys.exit(0)
if mark_mode:
    print(f"{newest:.6f}")
    sys.exit(0)
sys.exit(1)
DENIALS_PY
cat > "$WORKDIR/procs.py" <<'PROCS'
"""Print "PROCS <n> <pids>" for this user's other processes whose command line contains argv[1]."""

import os
import sys

pattern, me, uid = sys.argv[1], os.getpid(), os.getuid()
pids = []
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) == me:
        continue
    try:
        with open(f"/proc/{name}/status") as fh:
            owner = [int(u) for line in fh if line.startswith("Uid:") for u in line.split()[1:]]
        with open(f"/proc/{name}/stat", "rb") as fh:
            state = fh.read().rpartition(b")")[2].split()[0]
        with open(f"/proc/{name}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        continue
    if uid in owner and state not in (b"Z", b"X") and pattern in cmdline:
        pids.append(int(name))
print(f"PROCS {len(pids)} {','.join(map(str, sorted(pids)))}")
PROCS
cat > "$WORKDIR/under_claude.sh" <<'UNDER'
#!/bin/bash
# under_claude.sh CMD...: run CMD as a descendant of the claude binary and
# print its output, as "codex sandbox -- CMD" does for codex. Claude Code has
# no such subcommand, so CMD runs from a SessionStart hook, which stops
# claude once CMD is done, while claude still waits for the hook and before
# it sends its prompt.
set -u
if [[ "${1:-}" == --hook ]]; then
    mapfile -d '' args < "$2"
    "${args[@]}" > "$3.part" 2>&1
    mv "$3.part" "$3"
    pid=$$
    while [[ "$pid" -gt 1 ]]; do
        stat="$(< "/proc/$pid/stat")"
        read -r _ pid _ <<<"${stat##*) }"
        if [[ "$(readlink "/proc/$pid/exe")" == /usr/local/bin/claude ]]; then
            kill -TERM "$pid"
            break
        fi
    done
    exit 0
fi
dir="$(mktemp -d)"
printf '%s\0' "$@" > "$dir/args"
settings="$(python3 -c '
import json, sys
hook = {"type": "command", "command": sys.argv[1]}
print(json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}))
' "bash $0 --hook $dir/args $dir/out")"
# A scratch config dir and no plugin seed or sync, so claude starts no
# plugin MCP servers that would outlive it.
env -u CLAUDE_CODE_PLUGIN_SEED_DIR -u CLAUDE_CODE_SYNC_PLUGIN_INSTALL \
    CLAUDE_CONFIG_DIR="$dir/config" CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    timeout 60 claude -p --strict-mcp-config --settings "$settings" "Reply with OK." \
    >/dev/null 2>&1 || true
if [[ -f "$dir/out" ]]; then
    cat "$dir/out"
else
    echo "UNDER_CLAUDE: the SessionStart hook did not run"
fi
rm -rf "$dir"
UNDER
PROBE="/sandbox/$(basename "$WORKDIR")/probe.py"
PROCS="/sandbox/$(basename "$WORKDIR")/procs.py"
UNDER_CLAUDE="/sandbox/$(basename "$WORKDIR")/under_claude.sh"
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
print_header "=== 2. Profile (npm, goproxy, pypi): setup phase ==="
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
assert_ok "profile: the raw endpoints for preset hosts are replaced, with a warning" \
    grep -q "WARNING: 2 egress endpoint(s) for an egress preset host replaced" "$PROFILE_LOG"
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
ORPHAN_MARK="$(denial_mark "$PY_CALLER" registry.npmjs.org)"
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
if grep -q "^PROBE BLOCKED" <<<"$ORPHAN_RESULT" &&
    denial_logged "$ORPHAN_MARK" "$PY_CALLER" registry.npmjs.org >/dev/null; then
    pass "setup: npm from a process orphaned after the shim exited (DENIED, logged)"
else
    fail "setup: npm from a process orphaned after the shim exited: expected DENIED with a log line"
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
    checks[f"{n}_preset_rules_l7_read_only_enforced"] = all(
        {k: ep.get(k) for k in ("access", "protocol", "enforcement")}
        == {"access": "read-only", "protocol": "rest", "enforcement": "enforce"}
        for v in phase_rules(n).values()
        for ep in v["endpoints"]
    )


# The defaults hold pypi.org and files.pythonhosted.org as L4 endpoints, and
# the profile's raw endpoints cover registry.npmjs.org and (by wildcard)
# storage.googleapis.com with write access; each preset host must still be a
# single L7 read-only endpoint, for the agent and for the shim.
preset_hosts = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "proxy.golang.org",
    "storage.googleapis.com",
)
for name, where in (("0-create", "agent"), ("1-setup", "setup"), ("3-validate", "validate")):
    for host in preset_hosts:
        found = [
            ep
            for rule_name, v in rules[name].items()
            if where == "agent" or rule_name.startswith("agentic_ci_phase_")
            for ep in v["endpoints"]
            if ep.get("host") == host
        ]
        checks[f"{where}_{host}_single_l7_read_only_endpoint"] = len(found) == 1 and all(
            (ep.get("access"), ep.get("protocol"), ep.get("enforcement"))
            == ("read-only", "rest", "enforce")
            for ep in found
        )
checks["raw_wildcard_for_a_preset_host_never_applied"] = not any(
    ep.get("host") == "*.googleapis.com"
    for n in names
    for v in rules[n].values()
    for ep in v["endpoints"]
)
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

# ============================================================================
print_header "=== 6. Presets are read-only at L7 ==="
# Reads succeed, including the Go proxy's redirect of a module zip to Cloud
# Storage. Writes to the preset hosts get the proxy's own 403 (L7DENIED), not
# a CONNECT refusal and not a remote error: the request never leaves the
# sandbox. The write targets do not exist; without L7 enforcement the remote
# would answer them (ALLOWED http=404).
NPM_PKG_URL=https://registry.npmjs.org/is-number
GO_LIST_URL=https://proxy.golang.org/rsc.io/quote/@v/list
GO_ZIP_REDIRECT_URL=https://proxy.golang.org/github.com/aws/aws-sdk-go/@v/v1.55.5.zip
PYPI_SIMPLE_URL=https://pypi.org/simple/six/
NPM_WRITE_URL=https://registry.npmjs.org/agentic-ci-e2e-l7-probe
GCS_WRITE_URL=https://storage.googleapis.com/agentic-ci-e2e-l7-probe/probe.txt

# l7_checks LABEL CMD_PREFIX...: run every L7 probe under CMD_PREFIX.
l7_checks() {
    local where="$1"; shift
    expect "ALLOWED http=20[06] host=registry.npmjs.org" "$where: GET npm package" -- \
        "$@" "${PY_PROBE[@]}" "$NPM_PKG_URL"
    expect "ALLOWED http=20[06] host=proxy.golang.org" "$where: GET Go module list" -- \
        "$@" "${PY_PROBE[@]}" "$GO_LIST_URL"
    expect "ALLOWED http=20[06] host=storage.googleapis.com" \
        "$where: GET Go module zip through the redirect to Cloud Storage" -- \
        "$@" "${PY_PROBE[@]}" "$GO_ZIP_REDIRECT_URL"
    expect "ALLOWED http=20[06] host=pypi.org" "$where: GET PyPI simple index" -- \
        "$@" "${PY_PROBE[@]}" "$PYPI_SIMPLE_URL"
    local method
    for method in PUT POST; do
        expect L7DENIED "$where: $method to registry.npmjs.org" -- \
            "$@" "${PY_PROBE[@]}" "$NPM_WRITE_URL" "$method"
        expect L7DENIED "$where: $method to storage.googleapis.com" -- \
            "$@" "${PY_PROBE[@]}" "$GCS_WRITE_URL" "$method"
    done
}

# expect_output PATTERN DESC -- CMD...: run CMD in the sandbox and look for
# PATTERN (grep -E) in its output.
expect_output() {
    local pattern="$1" desc="$2"; shift 3
    local out
    out="$(timeout 180 openshell sandbox exec --name "$SANDBOX" --no-tty -- "$@" 2>&1 || true)"
    if grep -qE "$pattern" <<<"$out"; then
        pass "$desc"
    else
        fail "$desc"
        echo "  Got: $(tr '\n' ' ' <<<"$out" | tail -c 400)"
    fi
}

# The reused sandbox is in the agent phase: the agent's preset rules apply.
l7_checks "agent, codex" "${CODEX_EXEC[@]}"

assert_ok "switch to the setup phase for the L7 checks" \
    driver phase setup --profile-json "$PROFILE"
l7_checks "setup, shim" "$SHIM"

# Real package managers through the shim. The sandbox image has npm and uv;
# pip and go are not in it (toolchains arrive with the profile's toolchains).
expect_output '^7\.0\.0$' "setup: npm view through the shim" -- \
    "$SHIM" npm view --cache /tmp/agentic-ci-e2e-npm is-number@7.0.0 version
expect_output 'is-number-7\.0\.0\.tgz' "setup: npm pack through the shim" -- \
    "$SHIM" bash -c 'cd /tmp && npm pack --cache /tmp/agentic-ci-e2e-npm is-number@7.0.0'
expect_output '^UV_OK$' "setup: uv pip install from PyPI through the shim" -- \
    "$SHIM" bash -c 'rm -rf /tmp/agentic-ci-e2e-uv &&
        uv pip install --quiet --no-cache --python python3 --target /tmp/agentic-ci-e2e-uv \
            six==1.16.0 && test -f /tmp/agentic-ci-e2e-uv/six.py && echo UV_OK'
# npm prints the error field of the proxy's JSON 403 body.
expect_output 'policy_denied' "setup: npm publish through the shim is refused by the proxy" -- \
    "$SHIM" bash -c 'd=/tmp/agentic-ci-e2e-publish && rm -rf "$d" && mkdir -p "$d" && cd "$d" &&
        printf "%s\n" "{\"name\": \"agentic-ci-e2e-l7-probe\", \"version\": \"0.0.0\"}" \
            > package.json &&
        printf "%s\n" "//registry.npmjs.org/:_authToken=npm_fakeagenticcie2e" > .npmrc &&
        npm publish --cache /tmp/agentic-ci-e2e-npm --userconfig .npmrc'

assert_ok "switch back to the agent phase after the L7 checks" \
    driver phase agent --profile-json "$PROFILE"

# ============================================================================
print_header "=== 7. Leftover processes are killed before a shim phase ==="
# sx CMD...: run CMD in the sandbox and print its output. --no-login-shell,
# as the backend's own execs use, so the ~/.bash_profile planted below only
# runs where a check wants it to.
sx() {
    timeout 90 openshell sandbox exec --name "$SANDBOX" --no-tty --no-login-shell -- "$@" 2>&1 ||
        true
}
# procs PATTERN: "<count> <pids>" of sandbox-user processes whose command line has PATTERN.
procs() { sx python3 "$PROCS" "$1" | sed -n 's/^PROCS //p'; }

MAIN_PID="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["main_process"]["pid"])' \
    "$AGENTIC_CI_OPENSHELL_STATE" 2>/dev/null || true)"
if [[ "$MAIN_PID" =~ ^[0-9]+$ ]]; then
    pass "identity records the sandbox's main process (pid $MAIN_PID)"
else
    fail "identity records the sandbox's main process"
fi
if [[ "$(procs "sleep infinity")" == "1 $MAIN_PID" ]]; then
    pass "the recorded main process is the sandbox's sleep infinity"
else
    fail "the recorded main process is the sandbox's sleep infinity: got $(procs "sleep infinity")"
fi

# What an agent could leave behind, started from a codex descendant: a nohup
# background job, a setsid daemon, a process with the main process's exact
# command line, and a setsid daemon that keeps running the shim to reach npm.
POLLER_OUT=/tmp/agentic-ci-e2e-poller.out
sx rm -f "$POLLER_OUT" >/dev/null
POLLER_MARK="$(denial_mark "$PY_CALLER" registry.npmjs.org)"
# shellcheck disable=SC2016 # expanded inside the sandbox
sx "${CODEX_EXEC[@]}" bash -c '
    nohup sleep 3601 >/dev/null 2>&1 &
    setsid -f sleep 3602 >/dev/null 2>&1
    nohup sleep infinity >/dev/null 2>&1 &
    setsid -f bash -c "while :; do \"\$1\" python3 \"\$2\" \"\$3\" >> \"\$4\" 2>&1; sleep 1; done" \
        agentic-ci-e2e-poller "$0" "$1" "$2" "$3" >/dev/null 2>&1
    echo LEFT_BEHIND' "$SHIM" "$PROBE" "$NPM_URL" "$POLLER_OUT" >/dev/null
sleep 4
assert_ok "agent: the nohup job outlives its exec" test "$(procs "sleep 3601" | cut -d' ' -f1)" = 1
assert_ok "agent: the setsid daemon outlives its exec" test "$(procs "sleep 3602" | cut -d' ' -f1)" = 1
assert_ok "agent: the shim poller outlives its exec" \
    test "$(procs agentic-ci-e2e-poller | cut -d' ' -f1)" = 1
assert_ok "agent: a second process has the main process's command line" \
    test "$(procs "sleep infinity" | cut -d' ' -f1)" = 2
# An agent can also write ~/.bash_profile, which a login-shell exec ("bash
# -lc", OpenShell's default) sources before its command: a shell function
# named /usr/bin/python3 forges the leftover scan's and the provider probe's
# output, and an EXIT trap starts a daemon once the command is done. The
# backend's execs pass --no-login-shell, so neither may ever run in a switch.
PROFILE_HITS=/tmp/agentic-ci-e2e-profile-hits
# shellcheck disable=SC2016 # expanded inside the sandbox
sx "${CODEX_EXEC[@]}" bash -c '
    rm -f "$0"
    if [ -e ~/.bash_profile ]; then cp -p ~/.bash_profile ~/.agentic-ci-e2e-bash_profile.orig; fi
    cat >> ~/.bash_profile <<EOF
function /usr/bin/python3 { echo PROFILE_FUNCTION >> $0; printf "CANDIDATES 1\nMAIN 1 1\nLEFT 0\nDETACHED\n"; }
trap "echo PROFILE_TRAP >> $0; setsid -f sleep 3605 >/dev/null 2>&1" EXIT
EOF
    echo PLANTED' "$PROFILE_HITS" >/dev/null
# Control: a login-shell exec runs the planted function and trap.
FORGED="$(timeout 90 openshell sandbox exec --name "$SANDBOX" --no-tty -- \
    /usr/bin/python3 -I -S -c 'print("REAL")' 2>&1 || true)"
sleep 1
if grep -q "^LEFT 0" <<<"$FORGED" && ! grep -q REAL <<<"$FORGED" &&
    [[ "$(sx cat "$PROFILE_HITS" | tr '\n' ' ')" == "PROFILE_FUNCTION PROFILE_TRAP " ]] &&
    [[ "$(procs "sleep 3605" | cut -d' ' -f1)" == 1 ]]; then
    pass "agent: control: a login-shell exec runs the planted python3 function and EXIT trap"
else
    fail "agent: control: a login-shell exec runs the planted python3 function and EXIT trap"
    echo "  Got: $(tr '\n' ' ' <<<"$FORGED" | cut -c1-200) hits: $(sx cat "$PROFILE_HITS" | tr '\n' ' ')"
fi
sx rm -f "$PROFILE_HITS" >/dev/null

POLLER_BEFORE="$(sx cat "$POLLER_OUT")"
if grep -q "^PROBE BLOCKED" <<<"$POLLER_BEFORE" && ! grep -q "^PROBE ALLOWED" <<<"$POLLER_BEFORE" &&
    denial_logged "$POLLER_MARK" "$PY_CALLER" registry.npmjs.org >/dev/null; then
    pass "agent: the leftover gets nothing through the shim (DENIED, logged)"
else
    fail "agent: the leftover gets nothing through the shim"
    echo "  Got: ${POLLER_BEFORE:0:240}"
fi

VALIDATE_LOG="$TMPDIR_E2E/phase-validate-leftovers.log"
if driver phase validate --profile-json "$PROFILE" > "$VALIDATE_LOG" 2>&1; then
    pass "switch to the validate phase with leftovers running"
else
    fail "switch to the validate phase with leftovers running"
    cat "$VALIDATE_LOG"
fi
assert_ok "validate: the switch reports the killed leftovers" \
    grep -qE "Stopped ([5-9]|[1-9][0-9]+) leftover sandbox process\(es\) before the validate phase" \
    "$VALIDATE_LOG"
if [[ "$(sx sh -c "test -e $PROFILE_HITS && cat $PROFILE_HITS || echo NONE")" == NONE ]]; then
    pass "validate: the planted ~/.bash_profile never ran in the switch's execs"
else
    fail "validate: the planted ~/.bash_profile never ran in the switch's execs"
    echo "  Got: $(sx cat "$PROFILE_HITS" | tr '\n' ' ')"
fi
assert_ok "validate: the switch confirmed the detach with the real probe" \
    grep -q "API key provider detached for the validate phase" "$VALIDATE_LOG"
for marker in "sleep 3601" "sleep 3602" "sleep 3605" agentic-ci-e2e-poller; do
    got="$(procs "$marker")"
    if [[ "$got" == "0 " ]]; then pass "validate: '$marker' is gone"; else
        fail "validate: '$marker' is gone: got $got"; fi
done
got="$(procs "sleep infinity")"
if [[ "$got" == "1 $MAIN_PID" ]]; then
    pass "validate: only the recorded main process is left, the look-alike is gone"
else
    fail "validate: only the recorded main process is left: got $got"
fi
POLLER_AFTER="$(sx cat "$POLLER_OUT")"
sleep 5
POLLER_LATER="$(sx cat "$POLLER_OUT")"
if [[ "$POLLER_LATER" == "$POLLER_AFTER" ]] && ! grep -q "^PROBE ALLOWED" <<<"$POLLER_LATER"; then
    pass "validate: the leftover never reached npm through the shim, and stopped"
else
    fail "validate: the leftover never reached npm through the shim, and stopped"
    echo "  Got: $(tail -3 <<<"$POLLER_LATER" | tr '\n' ' ' | cut -c1-240)"
fi
# Put ~/.bash_profile back, so the plain execs below run as before.
# shellcheck disable=SC2016 # expanded inside the sandbox
sx bash -c 'if [ -e ~/.agentic-ci-e2e-bash_profile.orig ]; then
        mv ~/.agentic-ci-e2e-bash_profile.orig ~/.bash_profile; else rm -f ~/.bash_profile; fi' \
    >/dev/null
assert_ok "validate: the planted ~/.bash_profile is removed again" \
    test -z "$(sx sh -c 'grep -l agentic-ci-e2e-profile-hits ~/.bash_profile 2>/dev/null')"
assert_ok "validate: the sandbox still runs a new exec" \
    openshell sandbox exec --name "$SANDBOX" --no-tty -- true
expect ALLOWED "validate: npm from a new shim exec" -- "$SHIM" "${PY_PROBE[@]}" "$NPM_URL"

# ============================================================================
# key_provider_checks: detach, placeholder and re-attach checks for one API
# key provider, run by sections 8 (openai, codex) and 9 (api-key, claude).
# Set before calling: HARNESS and HARNESS_IMAGE (for driver), AGENT (label),
# AGENT_EXEC (runs a command under the agent binary), KEY_VAR (the provider
# placeholder's variable), KEY_HOST (the API host), KEY_PROBE (calls the API
# with its first argument or $KEY_VAR) and INJECTED (what the API answers
# when the proxy injected the fake key).
#
# What curl prints when no rule admits it to the API host: OpenShell v0.0.116
# answers the CONNECT with 403, v0.1.x refuses the connect. Either counts only
# with the sandbox log's DENIED line for curl and the host; anything else (a
# DNS failure, a timeout, curl missing) proves nothing.
DENIED_403="CONNECT tunnel failed, response 403"

# key_check WANT DESC -- CMD...: WANT is "injected" or "not-injected".
key_check() {
    local want="$1" desc="$2"; shift 3
    local out mark="" refused="" injected="" logged=""
    if [[ "$want" == not-injected ]]; then
        mark="$(denial_mark "$CURL_CALLER" "$KEY_HOST")"
    fi
    out="$(timeout 90 openshell sandbox exec --name "$SANDBOX" --no-tty --no-login-shell -- \
        "$@" 2>&1 || true)"
    out="$(tr '\n' ' ' <<<"$out" | cut -c1-200)"
    if grep -qF "$DENIED_403" <<<"$out"; then
        refused=proxy-403
    elif grep -qF "curl: (7) Failed to connect to $KEY_HOST" <<<"$out"; then
        refused=connect
    fi
    grep -qF "$INJECTED" <<<"$out" && injected=1
    if [[ "$want" == not-injected && -n "$refused" && -z "$injected" ]] &&
        denial_logged "$mark" "$CURL_CALLER" "$KEY_HOST" >/dev/null; then
        logged=1
    fi
    if { [[ "$want" == injected && -n "$injected" ]]; } || [[ -n "$logged" ]]; then
        pass "$desc ($want${logged:+: $refused, logged})"
    else
        fail "$desc: expected $want"
        echo "  Got: $out"
        # Only a refusal with no injected answer was looked up in the log.
        if [[ "$want" == not-injected && -n "$refused" && -z "$injected" ]]; then
            echo "  Sandbox log: no DENIED line for curl -> $KEY_HOST after $mark"
        fi
    fi
    return 0
}

# wait_key_env attached|detached: poll a fresh exec (30 s at most) until
# $KEY_VAR holds a provider placeholder, or none.
wait_key_env() {
    local value
    for _ in $(seq 15); do
        value="$(sx printenv "$KEY_VAR" | tr -d '\r\n')"
        if [[ "$1" == attached && "$value" == openshell:resolve:env:* ]] ||
            [[ "$1" == detached && -z "$value" ]]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

key_provider_checks() {
    local phase placeholder phase_log control_log agent_log reattach_log
    local a="$AGENT"
    assert_ok "$a: switch to the agent phase" driver phase agent --profile-json "$PROFILE"
    placeholder="$(sx printenv "$KEY_VAR" | tr -d '\r\n')"
    assert_ok "$a: agent: a new exec gets the $KEY_VAR placeholder" \
        grep -q '^openshell:resolve:env:' <<<"$placeholder"
    key_check injected "$a: agent: $a -> curl" -- "${AGENT_EXEC[@]}" "${KEY_PROBE[@]}"

    for phase in setup validate; do
        phase_log="$TMPDIR_E2E/phase-$phase-provider-$HARNESS.log"
        if driver phase "$phase" --profile-json "$PROFILE" > "$phase_log" 2>&1; then
            pass "$a: switch to the $phase phase"
        else
            fail "$a: switch to the $phase phase"
            cat "$phase_log"
        fi
        assert_ok "$a: $phase: the switch confirmed the detach" \
            grep -q "API key provider detached for the $phase phase" "$phase_log"
        assert_ok "$a: $phase: a new exec gets no provider placeholder" \
            test -z "$(sx printenv "$KEY_VAR" | tr -d '\r\n')"
        key_check not-injected "$a: $phase: plain curl" -- "${KEY_PROBE[@]}"
        key_check not-injected "$a: $phase: plain curl with the agent-phase placeholder" -- \
            "${KEY_PROBE[@]}" "$placeholder"
        key_check not-injected "$a: $phase: $a -> curl" -- "${AGENT_EXEC[@]}" "${KEY_PROBE[@]}"
        key_check not-injected "$a: $phase: $a -> curl with the agent-phase placeholder" -- \
            "${AGENT_EXEC[@]}" "${KEY_PROBE[@]}" "$placeholder"
        key_check not-injected "$a: $phase: shim -> $a -> curl with the agent-phase placeholder" -- \
            "$SHIM" "${AGENT_EXEC[@]}" "${KEY_PROBE[@]}" "$placeholder"
        if [[ "$phase" == setup ]]; then
            # Control: the refusals above come from the detach. With the provider
            # attached in the setup phase (agent rules parked), the rule
            # composed from its profile still admits the agent binary and the
            # key is injected.
            assert_ok "$a: setup control: attach the provider by hand" \
                openshell sandbox provider attach "$SANDBOX" ci-gcp
            assert_ok "$a: setup control: a new exec gets the placeholder again" \
                wait_key_env attached
            key_check injected "$a: setup control: $a -> curl with the provider attached" -- \
                "${AGENT_EXEC[@]}" "${KEY_PROBE[@]}"
            control_log="$TMPDIR_E2E/phase-setup-control-$HARNESS.log"
            if driver phase setup --profile-json "$PROFILE" > "$control_log" 2>&1; then
                pass "$a: setup control: switch to the setup phase again"
            else
                fail "$a: setup control: switch to the setup phase again"
                cat "$control_log"
            fi
            assert_ok "$a: setup control: the switch detached the provider it saw attached" \
                grep -q "API key provider detached for the setup phase" "$control_log"
            key_check not-injected "$a: setup control: $a -> curl after the switch" -- \
                "${AGENT_EXEC[@]}" "${KEY_PROBE[@]}"
        else
            # From setup the provider is detached; the switch attaches it
            # first, so the detach it confirms is a change it observed.
            assert_ok "$a: $phase: the provider not seen attached was attached before the detach" \
                grep -q "API key provider attached for the $phase phase" "$phase_log"
        fi
    done

    agent_log="$TMPDIR_E2E/phase-agent-provider-$HARNESS.log"
    if driver phase agent --profile-json "$PROFILE" > "$agent_log" 2>&1; then
        pass "$a: switch back to the agent phase"
    else
        fail "$a: switch back to the agent phase"
        cat "$agent_log"
    fi
    assert_ok "$a: agent again: the switch confirmed the attach" \
        grep -q "API key provider attached for the agent phase" "$agent_log"
    key_check injected "$a: agent again: $a -> curl authenticates after the re-attach" -- \
        "${AGENT_EXEC[@]}" "${KEY_PROBE[@]}"

    # A run that died in a shim phase leaves the provider detached; reusing
    # the sandbox attaches it again before the agent starts.
    assert_ok "$a: reuse: detach the provider by hand, as a run that died in validate leaves it" \
        openshell sandbox provider detach "$SANDBOX" ci-gcp
    # The supervisor picks the detach up within about 10 s; the reuse must
    # start from a sandbox that is really detached, or it proves nothing.
    assert_ok "$a: reuse: a new exec no longer gets the placeholder" wait_key_env detached
    reattach_log="$TMPDIR_E2E/setup-reuse-detached-$HARNESS.log"
    if driver setup --profile-json "$PROFILE" > "$reattach_log" 2>&1; then
        pass "$a: reuse a sandbox left with the provider detached"
    else
        fail "$a: reuse a sandbox left with the provider detached"
        cat "$reattach_log"
    fi
    assert_ok "$a: reuse: the existing sandbox is kept" \
        grep -q "Sandbox already exists" "$reattach_log"
    assert_ok "$a: reuse: the provider is attached again" \
        grep -q "API key provider attached for the agent phase" "$reattach_log"
    key_check injected "$a: reuse: $a -> curl authenticates" -- \
        "${AGENT_EXEC[@]}" "${KEY_PROBE[@]}"
}

# ============================================================================
print_header "=== 8. The OpenAI key provider is detached during setup and validate ==="
AGENT=codex
AGENT_EXEC=("${CODEX_EXEC[@]}")
KEY_VAR=OPENAI_API_KEY
KEY_HOST=api.openai.com
# OpenAI's 401 echoes the key masked, which shows the proxy injected it.
# shellcheck disable=SC2016 # expanded inside the sandbox
KEY_PROBE=(bash -c 'curl -sS --max-time 20 https://api.openai.com/v1/models \
    -H "Authorization: Bearer ${1:-$OPENAI_API_KEY}" 2>&1 | head -c 200' key-probe)
INJECTED="Incorrect API key provided: sk-e2e"
key_provider_checks

# ============================================================================
print_header "=== 9. The Anthropic key provider (api-key auth) is detached too ==="
# The same checks for the Claude Code harness with an Anthropic API key: the
# agentic-ci-anthropic provider, ANTHROPIC_API_KEY in the exec env and the
# claude binary. Switching harness and auth mode recreates the sandbox and
# the provider.
HARNESS=claude-code
HARNESS_IMAGE="$CLAUDE_SANDBOX"
export ANTHROPIC_API_KEY="$FAKE_ANTHROPIC_API_KEY"
CLAUDE_LOG="$TMPDIR_E2E/setup-claude.log"
if driver setup --profile-json "$PROFILE" > "$CLAUDE_LOG" 2>&1; then
    pass "claude: sandbox created with api-key auth and the profile"
    assert_ok "claude: the auth mode change recreated the sandbox and provider" \
        grep -q "Auth mode changed; recreating OpenShell sandbox and provider" "$CLAUDE_LOG"
    AGENT=claude
    AGENT_EXEC=(bash "$UNDER_CLAUDE")
    ANCESTOR_SCRIPT='
import os
pid = os.getppid()
while pid > 1:
    if os.readlink(f"/proc/{pid}/exe") == "/usr/local/bin/claude":
        print("UNDER_CLAUDE_BINARY")
        break
    with open(f"/proc/{pid}/stat") as fh:
        pid = int(fh.read().rpartition(")")[2].split()[1])
'
    KEY_VAR=ANTHROPIC_API_KEY
    KEY_HOST=api.anthropic.com
    # The rules match a caller by its ancestors' binaries, so AGENT_EXEC must
    # put the claude binary in the parent chain, as codex sandbox does.
    assert_ok "claude: the wrapper runs a command under the claude binary" \
        grep -q UNDER_CLAUDE_BINARY <<<"$(sx "${AGENT_EXEC[@]}" python3 -c "$ANCESTOR_SCRIPT")"
    # Anthropic's 401 never echoes the key. The fake key, which is key-shaped,
    # gets "API key is invalid."; an unresolved placeholder gets "invalid
    # x-api-key" instead, so only an injected key gives INJECTED.
    # shellcheck disable=SC2016 # expanded inside the sandbox
    KEY_PROBE=(bash -c 'curl -sS --max-time 20 https://api.anthropic.com/v1/models \
        -H "x-api-key: ${1:-$ANTHROPIC_API_KEY}" -H "anthropic-version: 2023-06-01" 2>&1 |
        head -c 200' key-probe)
    INJECTED="API key is invalid."
    key_provider_checks
else
    fail "claude: sandbox created with api-key auth and the profile"
    cat "$CLAUDE_LOG"
fi

echo ""
print_header "=== All test sections complete ==="
