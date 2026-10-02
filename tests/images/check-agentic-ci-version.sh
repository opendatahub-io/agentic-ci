#!/bin/bash
# check-agentic-ci-version.sh -- Check the agentic-ci version installed in an image.
#
# The images install agentic-ci from a build context without git metadata, so
# the version comes from the AGENTIC_CI_VERSION build argument. This fails when
# the image reports anything else, such as the 0.0.0+unknown fallback.
#
# Usage: check-agentic-ci-version.sh IMAGE EXPECTED_VERSION
# PODMAN overrides the podman command (for tests).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tests/images/shell-utils.sh
source "$SCRIPT_DIR/shell-utils.sh"

if [[ $# -ne 2 || -z "$1" || -z "$2" ]]; then
    print_error "Usage: $0 IMAGE EXPECTED_VERSION"
    exit 2
fi
image="$1"
expected="$2"
podman_cmd="${PODMAN:-podman}"

# `agentic-ci --version` prints "agentic-ci <version>" from the installed
# package metadata, whichever Python the image installed it into.
if ! output=$("$podman_cmd" run --rm --entrypoint agentic-ci "$image" --version); then
    print_error "Could not run agentic-ci --version in $image"
    exit 1
fi
actual="${output##* }"

if [[ "$actual" != "$expected" ]]; then
    print_error "$image has agentic-ci $actual, expected $expected"
    exit 1
fi
print_success "$image has agentic-ci $actual"
