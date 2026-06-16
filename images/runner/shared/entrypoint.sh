#!/bin/bash
# Container entrypoint for AI coding agent runner images.
#
# Handles GCP/Vertex AI credential setup from environment variables
# so standalone callers only need to pass env vars. When used with
# agentic-ci, this entrypoint is overridden (--entrypoint sleep).
#
# Auth methods:
#   Vertex AI:  GCP_SERVICE_ACCOUNT_KEY (raw JSON or base64)
#               AIPCC_CICD_GCP_SERVICE_ACCOUNT_KEY / AIPCC_CICD_GCP_PROJECT_ID
#               take precedence over GCP_* if defined.
#   Direct:     ANTHROPIC_API_KEY (no setup needed)

set -euo pipefail

_is_json() {
    printf '%s\n' "$1" | python3 -m json.tool >/dev/null 2>&1
}

_setup_vertex() {
    local creds=""

    if [[ -n "${GCP_SERVICE_ACCOUNT_KEY:-}" ]]; then
        if _is_json "$GCP_SERVICE_ACCOUNT_KEY"; then
            creds="$GCP_SERVICE_ACCOUNT_KEY"
        else
            local decoded
            decoded=$(printf '%s\n' "$GCP_SERVICE_ACCOUNT_KEY" | base64 -d 2>/dev/null) || true
            if [[ -n "$decoded" ]] && _is_json "$decoded"; then
                creds="$decoded"
            else
                echo "ERROR: GCP_SERVICE_ACCOUNT_KEY is not valid JSON or base64-encoded JSON" >&2
                exit 1
            fi
        fi
    fi

    [[ -z "$creds" ]] && return 1

    if [[ -n "${GCP_PROJECT_ID:-}" && -z "${ANTHROPIC_VERTEX_PROJECT_ID:-}" ]]; then
        export ANTHROPIC_VERTEX_PROJECT_ID="$GCP_PROJECT_ID"
    fi

    local gcloud_dir="$HOME/.config/gcloud"
    mkdir -p "$gcloud_dir/configurations"

    cat > "$gcloud_dir/configurations/config_default" << EOF
[core]
project = ${ANTHROPIC_VERTEX_PROJECT_ID:-}
disable_prompts = true
EOF

    printf '%s\n' "$creds" > "$gcloud_dir/application_default_credentials.json"
    chmod 600 "$gcloud_dir/application_default_credentials.json"

    export CLOUD_ML_REGION="${CLOUD_ML_REGION:-global}"
    _VERTEX_CONFIGURED=1
}

_detect_tool() {
    local cmd="${1:-}"
    case "$cmd" in
        claude|claude-code)
            export DISABLE_AUTOUPDATER=1
            if [[ "${_VERTEX_CONFIGURED:-}" == "1" ]]; then
                export CLAUDE_CODE_USE_VERTEX=1
            fi
            ;;
        opencode)
            export OPENCODE_DISABLE_AUTOUPDATE=1
            ;;
        *)
            if [[ "${AGENT_TOOL:-}" == "claude" ]]; then
                export DISABLE_AUTOUPDATER=1
                if [[ "${_VERTEX_CONFIGURED:-}" == "1" ]]; then
                    export CLAUDE_CODE_USE_VERTEX=1
                fi
            elif [[ "${AGENT_TOOL:-}" == "opencode" ]]; then
                export OPENCODE_DISABLE_AUTOUPDATE=1
            fi
            ;;
    esac
}

# Allow sourcing for tests without executing main logic
if [[ "${1:-}" == "--source-only" ]]; then
    return 0 2>/dev/null || exit 0
fi

# AIPCC_CICD_GCP_* variants take precedence over GCP_* if defined.
if [[ -n "${AIPCC_CICD_GCP_SERVICE_ACCOUNT_KEY:-}" ]]; then
    export GCP_SERVICE_ACCOUNT_KEY="$AIPCC_CICD_GCP_SERVICE_ACCOUNT_KEY"
fi
if [[ -n "${AIPCC_CICD_GCP_PROJECT_ID:-}" ]]; then
    export GCP_PROJECT_ID="$AIPCC_CICD_GCP_PROJECT_ID"
fi

_VERTEX_CONFIGURED=0
if [[ -n "${GCP_SERVICE_ACCOUNT_KEY:-}" ]]; then
    _setup_vertex
fi

_detect_tool "${1:-}"
agentic-ci enable-plugins

# If the first argument is the tool command itself (e.g. "claude", "opencode"),
# pass through as-is. Otherwise, the caller passed only flags/args and expects
# the entrypoint to prepend the tool command (matching the runner image
# behavior where the entrypoint always prepends the agent command).
case "${1:-}" in
    claude|claude-code|opencode|bash|sh|true)
        exec "$@"
        ;;
    *)
        exec "${AGENT_TOOL:-claude}" "$@"
        ;;
esac
