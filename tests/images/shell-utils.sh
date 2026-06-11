#!/bin/bash
# shell-utils.sh -- Shared shell utilities for agentic CI projects.
#
# Source this file to get colored output functions, dependency checking,
# and other common helpers.
#
# Usage:
#   source /path/to/shell-utils.sh

# Color constants
readonly AGENTIC_RED='\033[0;31m'
readonly AGENTIC_GREEN='\033[0;32m'
readonly AGENTIC_YELLOW='\033[1;33m'
readonly AGENTIC_BLUE='\033[0;34m'
readonly AGENTIC_CYAN='\033[0;36m'
readonly AGENTIC_NC='\033[0m'

# Colored output functions
print_step()    { echo -e "${AGENTIC_BLUE}▶ $1${AGENTIC_NC}"; }
print_success() { echo -e "${AGENTIC_GREEN}✓ $1${AGENTIC_NC}"; }
print_warning() { echo -e "${AGENTIC_YELLOW}⚠ $1${AGENTIC_NC}"; }
print_error()   { echo -e "${AGENTIC_RED}✗ $1${AGENTIC_NC}"; }
print_header()  { echo -e "${AGENTIC_CYAN}$1${AGENTIC_NC}"; }

# Check that all required commands are available.
# Usage: check_dependencies podman python3 git curl jq
check_dependencies() {
    local missing=()
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -ne 0 ]]; then
        print_error "Missing dependencies: ${missing[*]}"
        return 1
    fi
}

# URL-encode a string for use in API paths.
urlencode() {
    python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$1"
}
