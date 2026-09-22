"""Policy resolution for OpenShell sandbox."""

import os

import yaml

REPO_POLICY_PATH = ".agentic-ci/openshell-policy.yml"

# Base sandbox policy passed to ``openshell sandbox create --policy``.
#
# Mirrors OpenShell's restrictive default policy (openshell_policy::
# restrictive_default_policy). Passing it explicitly keeps the supervisor
# from discovering the image's own /etc/openshell/policy.yaml: the
# Hummingbird agentic images ship one in a foreign schema, and OpenShell
# v0.0.116-rhaiv.8+ rejects the sandbox with "Image policy is invalid"
# instead of falling back to its default. Network endpoints and binaries
# are layered on afterwards with ``openshell policy update``.
BASE_POLICY = {
    "version": 1,
    "filesystem_policy": {
        "include_workdir": True,
        "read_only": ["/usr", "/lib", "/proc", "/dev/urandom", "/app", "/etc", "/var/log"],
        "read_write": ["/tmp", "/dev/null"],
    },
    "landlock": {"compatibility": "best_effort"},
}

# Default network endpoints in openshell policy update format:
#   host:port:access[:protocol[:enforcement]]
# No protocol is specified so endpoints are L4-only (CONNECT tunneling).
#
# Inference hosts are deliberately absent. The attached provider profile
# (backends/openshell/profiles/*.yaml) contributes them as an L7 ``rest``
# layer with the harness binaries, and the supervisor proxy resolves the
# credential placeholder there. Declaring the same host again here makes
# the gateway reject the update with "network endpoint ambiguity validation
# failed ... conflicting metadata".
DEFAULT_ENDPOINTS = [
    "github.com:443:full",
    "*.github.com:443:full",
    "gitlab.com:443:full",
    "*.gitlab.com:443:full",
    "pypi.org:443:read-only",
    "files.pythonhosted.org:443:read-only",
]

# Extra endpoints per auth mode, beyond what the provider profile supplies.
AUTH_ENDPOINTS = {
    # google-vertex-ai profile: *-aiplatform / aiplatform / *.rep hosts.
    "vertex": [],
    # anthropic profile: api.anthropic.com.
    "api-key": [],
    # openai profile: api.openai.com. Codex's ChatGPT backend API is served
    # under chatgpt.com/backend-api; OpenShell policies match hosts, not URL
    # paths, and no profile declares that host.
    "openai": [
        "chatgpt.com:443:read-write",
    ],
}


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


def resolve_endpoints(flag_path=None, workdir=".", auth_mode=None):
    """Resolve the endpoint list to use for policy update.

    Merges the built-in defaults and endpoints required by *auth_mode* with
    extra endpoints from, in priority order:

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
    endpoints.extend(AUTH_ENDPOINTS.get(auth_mode, []))
    seen = set(endpoints)
    for ep in extra:
        if ep not in seen:
            endpoints.append(ep)
            seen.add(ep)
    return endpoints
