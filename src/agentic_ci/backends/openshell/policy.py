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
    "*.gitlab.com:443:full",
    "pypi.org:443:read-only",
    "files.pythonhosted.org:443:read-only",
    "aiplatform.googleapis.com:443:read-write",
    "*.aiplatform.googleapis.com:443:read-write",
    "oauth2.googleapis.com:443:read-write",
    "api.anthropic.com:443:read-write",
]

CURSOR_ENDPOINTS = [
    "api2.cursor.sh:443:read-write",
    "*.cursor.sh:443:read-write",
    "*.api5.cursor.sh:443:read-write",
    "*.us.api5.cursor.sh:443:read-write",
]

_CURSOR_TLS_SKIP_HOSTS = {endpoint.split(":", 1)[0] for endpoint in CURSOR_ENDPOINTS}


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


def _extract_tls_skip_hosts(tls_skip_hosts):
    hosts = set()
    for item in tls_skip_hosts or []:
        if isinstance(item, tuple) and item:
            host = str(item[0]).strip()
        elif isinstance(item, str):
            host = item.split(":", 1)[0].strip()
        else:
            continue
        if host:
            hosts.add(host)
    return hosts


def _should_include_cursor_defaults(tls_skip_hosts):
    skip_hosts = _extract_tls_skip_hosts(tls_skip_hosts)
    return bool(skip_hosts & _CURSOR_TLS_SKIP_HOSTS)


def resolve_endpoints(flag_path=None, workdir=".", tls_skip_hosts=None):
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
    if _should_include_cursor_defaults(tls_skip_hosts):
        endpoints.extend(CURSOR_ENDPOINTS)
    seen = set(endpoints)
    for ep in extra:
        if ep not in seen:
            endpoints.append(ep)
            seen.add(ep)
    return endpoints


def _parse_endpoint_string(ep_str):
    """Parse ``host:port:access[:protocol[:enforcement]]`` into a dict."""
    parts = ep_str.split(":")
    if len(parts) < 3:
        raise ValueError(
            f"Malformed endpoint {ep_str!r}: expected host:port:access[:protocol[:enforcement]]"
        )
    try:
        port = int(parts[1])
    except ValueError:
        raise ValueError(f"Malformed endpoint {ep_str!r}: port {parts[1]!r} is not a valid integer")
    entry = {"host": parts[0], "port": port, "access": parts[2]}
    if len(parts) > 3:
        entry["protocol"] = parts[3]
    if len(parts) > 4:
        entry["enforcement"] = parts[4]
    return entry


def generate_policy_yaml(endpoints, binaries, tls_skip_hosts=None, otel_port=None):
    """Generate a complete OpenShell policy YAML with optional ``tls: skip``.

    ``openshell policy update --add-endpoint`` does not support the ``tls``
    field, so harnesses that need it (see ``Harness.tls_skip_hosts``) require
    a full policy YAML applied via ``openshell policy set --policy <file>
    --wait``.  Since ``policy set`` replaces the *entire* policy, the YAML
    must include the sandbox defaults for the static sections (filesystem,
    landlock, process).

    *tls_skip_hosts* is a set of hostname strings whose endpoints should
    have ``tls: skip`` applied (the proxy acts as a raw TCP tunnel for
    those hosts, preserving end-to-end TLS negotiation).

    Returns the YAML as a string.
    """
    skip_set = set(tls_skip_hosts or [])
    binary_list = [{"path": b} for b in binaries]

    standard_eps = []
    skip_eps = []
    for ep_str in endpoints:
        entry = _parse_endpoint_string(ep_str)
        if entry["host"] in skip_set:
            entry["tls"] = "skip"
            skip_eps.append(entry)
        else:
            standard_eps.append(entry)

    if otel_port:
        standard_eps.append(
            {
                "host": "host.openshell.internal",
                "port": otel_port,
                "access": "read-write",
            }
        )

    network_policies = {}
    if standard_eps:
        network_policies["standard"] = {
            "name": "standard",
            "endpoints": standard_eps,
            "binaries": binary_list,
        }
    if skip_eps:
        network_policies["tls_skip"] = {
            "name": "tls-skip",
            "endpoints": skip_eps,
            "binaries": binary_list,
        }

    policy = {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": True,
            "read_only": ["/usr", "/lib", "/proc", "/dev/urandom", "/app", "/etc", "/var/log"],
            "read_write": ["/sandbox", "/tmp", "/dev/null"],
        },
        "landlock": {"compatibility": "best_effort"},
        "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
        "network_policies": network_policies,
    }

    return yaml.dump(policy, default_flow_style=False, sort_keys=False)
