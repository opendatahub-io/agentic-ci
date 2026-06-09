"""Policy resolution for OpenShell sandbox."""

import os

REPO_POLICY_PATH = ".agentic-ci/openshell-policy.yml"

# Default network endpoints in openshell policy update format:
#   host:port:access[:protocol[:enforcement]]
# No protocol is specified so endpoints are L4-only (CONNECT tunneling).
# Using protocol=rest would enable L7 inspection which blocks CONNECT
# requests that Vertex AI streaming/gRPC clients use.
DEFAULT_ENDPOINTS = [
    "github.com:443:full",
    "*.github.com:443:full",
    "gitlab.com:443:full",
    "pypi.org:443:read-only",
    "files.pythonhosted.org:443:read-only",
    "aiplatform.googleapis.com:443:read-write",
    "*.aiplatform.googleapis.com:443:read-write",
    "oauth2.googleapis.com:443:read-write",
    "api.anthropic.com:443:read-write",
]


def resolve_endpoints(flag_path=None):
    """Resolve the endpoint list to use for policy update.

    1. Explicit --policy flag path (parsed for endpoints — not yet supported,
       returns default endpoints)
    2. .agentic-ci/openshell-policy.yml in the workdir (same)
    3. Built-in default endpoints

    Returns a list of endpoint strings for ``openshell policy update --add-endpoint``.
    """
    if flag_path and os.path.isfile(flag_path):
        print(f"  Policy source: --policy flag ({os.path.abspath(flag_path)})", flush=True)
    else:
        print("  Policy source: built-in default", flush=True)

    return list(DEFAULT_ENDPOINTS)
