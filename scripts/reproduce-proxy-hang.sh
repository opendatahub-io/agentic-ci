#!/usr/bin/env bash
set -euo pipefail
#
# Reproduce OpenShell proxy hang / timeout issues.
#
# Sets up an OpenShell sandbox using agentic-ci (identical network
# policy, provider config, and CONNECT tunnel setup), then runs a
# probe script inside the sandbox that makes sustained HTTPS requests
# and reports hangs.
#
# While the probe runs, the gateway log is tailed for credential
# rotation events so you can correlate hangs with token refresh.
#
# Local usage (run from repo root):
#
#   # Build the CI image (has openshell, gateway, podman, agentic-ci)
#   make openshell-ci-build
#
#   # Quick 5-minute test
#   ./scripts/reproduce-proxy-hang.sh --duration 300
#
#   # 2-hour soak with Vertex AI calls
#   ./scripts/reproduce-proxy-hang.sh --vertex --duration 7200
#
#   # Test long-held connections (simulates long inference calls)
#   ./scripts/reproduce-proxy-hang.sh --mode sustained --interval 120
#
# The script auto-detects whether openshell is on PATH. If not, it
# builds the openshell CI image and re-execs itself inside a
# privileged container with the repo mounted at /workspace.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKDIR=""
GW_TAIL_PID=""

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --duration SEC   Total run time (default: 7200)"
    echo "  --interval SEC   Seconds between probes (default: 10)"
    echo "  --mode MODE      rapid|sustained (default: rapid)"
    echo "  --vertex         Include Vertex AI API probes"
    echo "  --image IMAGE    Sandbox image override"
    echo "  --no-reexec      Skip container re-exec even if openshell is missing"
    echo "  -h, --help       Show this help"
}

cleanup() {
    echo ""
    echo "--- Cleaning up ---"
    [[ -n "$GW_TAIL_PID" ]] && kill "$GW_TAIL_PID" 2>/dev/null || true
    if [[ -n "$WORKDIR" ]]; then
        agentic-ci stop --backend openshell --workdir "$WORKDIR" 2>/dev/null || true
        rm -rf "$WORKDIR"
    fi
}

# ── Parse arguments ──────────────────────────────────────────────────

DURATION=7200
INTERVAL=10
MODE=rapid
VERTEX=""
IMAGE=""
NO_REEXEC=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --duration)   DURATION="$2"; shift 2 ;;
        --interval)   INTERVAL="$2"; shift 2 ;;
        --mode)       MODE="$2";     shift 2 ;;
        --vertex)     VERTEX="--vertex"; shift ;;
        --image)      IMAGE="$2";    shift 2 ;;
        --no-reexec)  NO_REEXEC=true; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            echo "Unknown argument: $1"; usage; exit 1 ;;
    esac
done

# ── Auto-enter CI container if openshell is not available ────────────

if ! command -v openshell >/dev/null 2>&1 && [[ "$NO_REEXEC" == "false" ]]; then
    echo "=== openshell not found on PATH, entering CI container ==="
    echo ""

    CI_IMAGE="openshell:latest"
    if ! podman image exists "$CI_IMAGE" 2>/dev/null; then
        echo "Building CI image (make openshell-ci-build)..."
        make -C "$REPO_ROOT" openshell-ci-build
    fi

    # The CI image needs a custom supervisor with google-cloud provider
    # support. See e2e-openshell-sandbox.sh for the tracking issue.
    SUPERVISOR_IMAGE="${OPENSHELL_SUPERVISOR_IMAGE:-quay.io/mprpic/openshell-supervisor:pr1763}"

    # Forward all original arguments to the re-exec inside the container
    INNER_ARGS=(--no-reexec --duration "$DURATION" --interval "$INTERVAL" --mode "$MODE")
    [[ -n "$VERTEX" ]] && INNER_ARGS+=("$VERTEX")
    [[ -n "$IMAGE" ]] && INNER_ARGS+=(--image "$IMAGE")

    # Build volume mounts: repo + GCP credentials (if present)
    VOLUMES=(-v "${REPO_ROOT}:/workspace:z")
    ADC_PATH="$HOME/.config/gcloud/application_default_credentials.json"
    if [[ -f "$ADC_PATH" ]]; then
        VOLUMES+=(-v "${ADC_PATH}:/root/.config/gcloud/application_default_credentials.json:ro,z")
    fi

    # Collect env vars to forward
    ENV_ARGS=(-e "OPENSHELL_SUPERVISOR_IMAGE=${SUPERVISOR_IMAGE}")
    [[ -n "${ANTHROPIC_VERTEX_PROJECT_ID:-}" ]] && ENV_ARGS+=(-e "ANTHROPIC_VERTEX_PROJECT_ID")
    [[ -n "${CLOUD_ML_REGION:-}" ]] && ENV_ARGS+=(-e "CLOUD_ML_REGION")
    [[ -n "${GOOGLE_CLOUD_PROJECT:-}" ]] && ENV_ARGS+=(-e "GOOGLE_CLOUD_PROJECT")
    [[ -n "${GCP_PROJECT_ID:-}" ]] && ENV_ARGS+=(-e "GCP_PROJECT_ID")
    [[ -n "${VERTEX_LOCATION:-}" ]] && ENV_ARGS+=(-e "VERTEX_LOCATION")
    [[ -n "${ANTHROPIC_API_KEY:-}" ]] && ENV_ARGS+=(-e "ANTHROPIC_API_KEY")

    echo "  CI image:          $CI_IMAGE"
    echo "  Supervisor image:  $SUPERVISOR_IMAGE"
    echo "  Mounting repo:     $REPO_ROOT -> /workspace"
    [[ -f "$ADC_PATH" ]] && echo "  Mounting GCP ADC:  $ADC_PATH"
    echo ""

    # Re-install agentic-ci from the mounted repo so the probe scripts
    # and any local changes are picked up.
    exec podman run --rm -it \
        --privileged \
        --security-opt label=disable \
        "${VOLUMES[@]}" \
        "${ENV_ARGS[@]}" \
        "$CI_IMAGE" \
        bash -c '
            # subuid/subgid needed for rootless podman inside the container
            : > /etc/subuid; : > /etc/subgid
            echo "root:100000:65536" >> /etc/subuid
            echo "root:100000:65536" >> /etc/subgid

            # Reinstall agentic-ci from mounted source
            uv pip install --system --quiet /workspace 2>&1 | tail -1

            exec /workspace/scripts/reproduce-proxy-hang.sh '"$(printf "'%s' " "${INNER_ARGS[@]}")"'
        '
fi

# ── From here on, openshell is available (native or inside container) ─

trap cleanup EXIT

WORKDIR="$(mktemp -d /tmp/proxy-probe-XXXXXX)"
cp "$SCRIPT_DIR/proxy-probe.py" "$WORKDIR/"

echo "=== OpenShell proxy hang reproducer ==="
echo "  Mode:     $MODE"
echo "  Duration: ${DURATION}s"
echo "  Interval: ${INTERVAL}s"
echo "  Vertex:   ${VERTEX:-no}"
echo "  Workdir:  $WORKDIR"
echo ""

# Print component versions (same as e2e tests)
echo "--- Component versions ---"
echo "  agentic-ci:        $(agentic-ci --version 2>&1 || echo unknown)"
echo "  openshell:         $(openshell --version 2>&1 || echo unknown)"
echo "  openshell-gateway: $(openshell-gateway --version 2>&1 || echo unknown)"
echo "  podman:            $(podman --version 2>&1 || echo unknown)"
echo ""

# ── Set up sandbox ───────────────────────────────────────────────────

IMAGE_ARG=""
if [[ -n "$IMAGE" ]]; then
    IMAGE_ARG="--image $IMAGE"
fi

# shellcheck disable=SC2086
agentic-ci setup --backend openshell --workdir "$WORKDIR" $IMAGE_ARG

# ── Tail gateway log for rotation events ─────────────────────────────

GATEWAY_LOG=""
GATEWAY_LOG=$(find ~/.local/state/openshell -name 'gateway-*.log' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | head -1 | cut -d' ' -f2-) || true

if [[ -n "$GATEWAY_LOG" ]]; then
    echo ""
    echo "--- Gateway log: $GATEWAY_LOG ---"
    echo "    (filtering for rotate/refresh/error/timeout events)"
    echo ""
    tail -f "$GATEWAY_LOG" 2>/dev/null \
        | grep --line-buffered -iE 'rotat|refresh|error|timeout|disconnect|expire' \
        | sed 's/^/  [gateway] /' &
    GW_TAIL_PID=$!
fi

# ── Run probe inside sandbox ─────────────────────────────────────────

WORKDIR_BASE="$(basename "$WORKDIR")"
SANDBOX_PROBE="/sandbox/${WORKDIR_BASE}/proxy-probe.py"

echo ""
echo "=== Running probe inside sandbox ==="
echo ""

# shellcheck disable=SC2086
openshell sandbox exec --name ci --no-tty -- \
    python3 "$SANDBOX_PROBE" \
    --duration "$DURATION" \
    --interval "$INTERVAL" \
    --mode "$MODE" \
    $VERTEX

echo ""
echo "=== Probe complete ==="
