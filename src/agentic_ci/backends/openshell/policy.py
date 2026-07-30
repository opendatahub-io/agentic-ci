"""Policy resolution for OpenShell sandbox."""

import copy
import os

import yaml

from agentic_ci.backends.openshell.provider import PROVIDER_NAME

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
    "*.gitlab.com:443:full",
    "pypi.org:443:read-only",
    "files.pythonhosted.org:443:read-only",
]

AUTH_ENDPOINTS = {
    "vertex": [
        "aiplatform.googleapis.com:443:read-write",
        "*.aiplatform.googleapis.com:443:read-write",
        "oauth2.googleapis.com:443:read-write",
    ],
    "api-key": [
        "api.anthropic.com:443:read-write",
    ],
    "openai": [
        "api.openai.com:443:read-write",
        # Codex's ChatGPT backend API is served under chatgpt.com/backend-api.
        # OpenShell policies match hosts, not URL paths.
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


# GCP hosts that need credential_binding.provider for the endpointless
# google-cloud profile.
_GCP_CREDENTIAL_HOSTS = {
    "aiplatform.googleapis.com",
    "*.aiplatform.googleapis.com",
    "oauth2.googleapis.com",
}


def build_credential_binding_patch(policy_get_output, provider_name=PROVIDER_NAME):
    """Patch a policy to add credential_binding on GCP endpoints.

    Takes the JSON output of ``openshell policy get --base -o json``
    (which wraps the policy under a ``policy`` key), extracts the raw
    policy, adds ``credential_binding.provider`` to GCP endpoints, and
    returns the raw policy dict suitable for ``openshell policy set``.
    Returns None if no changes are needed.
    """
    raw_policy = policy_get_output.get("policy")
    if not isinstance(raw_policy, dict):
        return None

    patched = copy.deepcopy(raw_policy)
    network_policies = patched.get("network_policies")
    if not isinstance(network_policies, dict):
        return None

    changed = False
    for rule in network_policies.values():
        endpoints = rule.get("endpoints")
        if not isinstance(endpoints, list):
            continue
        for ep in endpoints:
            host = ep.get("host", "")
            if host in _GCP_CREDENTIAL_HOSTS and "credential_binding" not in ep:
                ep["credential_binding"] = {"provider": provider_name}
                changed = True

    return patched if changed else None
