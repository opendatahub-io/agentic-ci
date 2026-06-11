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

_enable_plugins() {
    local plugins_var="${AGENT_ENABLED_PLUGINS:-}"

    local claude_home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    local settings_path="$claude_home/settings.json"
    local installed_path="$claude_home/plugins/installed_plugins.json"

    if [[ ! -f "$installed_path" ]]; then
        if [[ -n "$plugins_var" ]]; then
            echo "WARNING: AGENT_ENABLED_PLUGINS is set but $installed_path not found" >&2
        fi
        return 0
    fi
    [[ ! -f "$settings_path" ]] && printf '{}\n' > "$settings_path"

    # Matching uses the name prefix before '@' (e.g. 'foo' from 'foo@mkt'),
    # so AGENT_ENABLED_PLUGINS=foo enables foo from all marketplaces.
    python3 -c "
import json, sys

plugins_csv = sys.argv[1]
settings_path = sys.argv[2]
installed_path = sys.argv[3]

with open(installed_path) as f:
    installed = json.load(f)
installed_keys = list(installed.get('plugins', {}).keys())

try:
    with open(settings_path) as f:
        settings = json.load(f)
except (json.JSONDecodeError, ValueError):
    print(f'WARNING: {settings_path} contains invalid JSON, resetting to empty', file=sys.stderr)
    settings = {}
enabled = settings.setdefault('enabledPlugins', {})

requested = set(p.strip() for p in plugins_csv.split(',') if p.strip()) if plugins_csv else set()

if not requested:
    for key in installed_keys:
        enabled[key] = True
else:
    enabled.clear()
    matched = set()
    for key in installed_keys:
        name = key.split('@')[0]
        if name in requested:
            enabled[key] = True
            matched.add(name)

    unmatched = sorted(requested - matched)
    if unmatched:
        if matched:
            safe_matched = ', '.join(sorted(matched))
            if len(safe_matched) > 200:
                safe_matched = safe_matched[:200] + '...'
            print(f'Matched: {safe_matched}', file=sys.stderr)
        safe_names = ', '.join(unmatched)
        if len(safe_names) > 200:
            safe_names = safe_names[:200] + '...'
        print(f'ERROR: unknown plugin(s) in AGENT_ENABLED_PLUGINS: {safe_names}', file=sys.stderr)
        sys.exit(1)

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')
" "$plugins_var" "$settings_path" "$installed_path"
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
_enable_plugins

# If the first argument is the tool command itself (e.g. "claude", "opencode"),
# pass through as-is. Otherwise, the caller passed only flags/args and expects
# the entrypoint to prepend the tool command (matching the runner image
# behavior where the entrypoint always prepends the agent command).
case "${1:-}" in
    claude|claude-code|opencode|bash|sh)
        exec "$@"
        ;;
    *)
        exec "${AGENT_TOOL:-claude}" "$@"
        ;;
esac
