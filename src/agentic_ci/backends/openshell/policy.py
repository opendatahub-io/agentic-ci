"""Policy resolution for OpenShell sandbox."""

import os

import yaml

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


def _load_endpoints_from_file(path):
    """Parse endpoint list from a YAML policy file."""
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        return []
    endpoints = data.get("endpoints", [])
    if not isinstance(endpoints, list):
        return []
    return [str(ep) for ep in endpoints]


def resolve_endpoints(flag_path=None, workdir="."):
    """Resolve the endpoint list to use for policy update.

    Merges the built-in defaults with extra endpoints from, in priority
    order:

    1. Explicit ``--policy`` flag path
    2. ``.agentic-ci/openshell-policy.yml`` in *workdir*

    Returns a list of endpoint strings for ``openshell policy update --add-endpoint``.
    """
    extra = []
    source = "built-in default"

    if flag_path and os.path.isfile(flag_path):
        extra = _load_endpoints_from_file(flag_path)
        source = f"--policy flag ({os.path.abspath(flag_path)})"
    else:
        repo_path = os.path.join(workdir, REPO_POLICY_PATH)
        if os.path.isfile(repo_path):
            extra = _load_endpoints_from_file(repo_path)
            source = f"repo ({os.path.abspath(repo_path)})"

    print(f"  Policy source: {source}", flush=True)

    endpoints = list(DEFAULT_ENDPOINTS)
    seen = set(endpoints)
    for ep in extra:
        if ep not in seen:
            endpoints.append(ep)
            seen.add(ep)
    return endpoints
