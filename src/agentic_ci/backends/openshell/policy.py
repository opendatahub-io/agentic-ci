"""Policy resolution for OpenShell sandbox."""

import copy
import os
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from types import MappingProxyType

import yaml

from agentic_ci import log
from agentic_ci.backends.openshell.provider import PROVIDER_NAME
from agentic_ci.sandbox_profile import CREDENTIAL_ENDPOINT_OPTIONS, SandboxProfile

REPO_POLICY_PATH = ".agentic-ci/openshell-policy.yml"

# Default network endpoints in openshell policy update format:
#   host:port:access[:protocol[:enforcement]]
# No protocol is specified so endpoints are L4-only (CONNECT tunneling).
# Using protocol=rest would enable L7 inspection which blocks CONNECT
# requests that Vertex AI streaming/gRPC clients use.
#
# Hosts that a provider profile marks as credentialed (api.anthropic.com for
# the anthropic provider, api.openai.com for the openai provider) reject
# L4-only rules since OpenShell v0.0.116 unless the endpoint explicitly opts
# in with the allow-uninspected-credentials option. This mirrors the
# allow_uninspected_credentials flag build_credential_binding_patch sets on
# the GCP endpoints.
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
        "api.anthropic.com:443:read-write:::allow-uninspected-credentials",
    ],
    # No provider backs a Claude subscription OAuth token, so the host
    # carries no provider credential and needs no uninspected opt-in.
    "oauth": [
        "api.anthropic.com:443:read-write",
    ],
    "openai": [
        "api.openai.com:443:read-write:::allow-uninspected-credentials",
        # Codex's ChatGPT backend API is served under chatgpt.com/backend-api.
        # OpenShell policies match hosts, not URL paths.
        "chatgpt.com:443:read-write",
    ],
}


EGRESS_PHASES = ("setup", "validate", "agent")
"""Sandbox phases with their own egress.

``setup`` and ``validate`` endpoints are bound only to the setup shim and are
switched with a whole-policy ``openshell policy set`` (see
``sandbox.apply_phase_policy``). ``agent`` endpoints are bound to the agent
binaries when the sandbox is created.
"""

_ALL_PHASES = frozenset(EGRESS_PHASES)
_SHIM_PHASES = ("setup", "validate")


@dataclass(frozen=True)
class EgressPreset:
    """A named set of endpoints a sandbox profile can open.

    ``endpoints`` use the ``openshell policy update --add-endpoint`` format.
    ``phases`` lists the :data:`EGRESS_PHASES` the preset is open in, so a
    later preset can be open only while setup runs.
    """

    endpoints: tuple[str, ...]
    phases: frozenset[str] = _ALL_PHASES


# Keep the names in step with sandbox_profile.KNOWN_EGRESS_PRESETS, which
# stays backend-neutral and does not import this module.
EGRESS_PRESETS: Mapping[str, EgressPreset] = MappingProxyType(
    {
        "pypi": EgressPreset(
            endpoints=(
                "pypi.org:443:read-only",
                "files.pythonhosted.org:443:read-only",
            ),
        ),
        "npm": EgressPreset(endpoints=("registry.npmjs.org:443:read-only",)),
        "goproxy": EgressPreset(
            endpoints=(
                "proxy.golang.org:443:read-only",
                "sum.golang.org:443:read-only",
                "storage.googleapis.com:443:read-only",
            ),
        ),
        "github-release-assets": EgressPreset(
            endpoints=(
                "release-assets.githubusercontent.com:443:read-only",
                "objects.githubusercontent.com:443:read-only",
                "raw.githubusercontent.com:443:read-only",
            ),
        ),
    }
)


def _profile_endpoints(profile: SandboxProfile, phase: str) -> list[str]:
    """Endpoints *profile* opens in *phase*: its presets open then, then its raw egress.

    Raw egress only comes from central, reviewed configuration and applies to
    every phase. Preset names this module does not know (possible only for a
    profile built without ``parse_profile``) are skipped with a warning.
    """
    endpoints: list[str] = []
    unknown = 0
    for name in profile.egress:
        preset = EGRESS_PRESETS.get(name)
        if preset is None:
            unknown += 1
            continue
        if phase in preset.phases:
            endpoints.extend(preset.endpoints)
    if unknown:
        log.info(f"WARNING: {unknown} unknown egress preset(s) in the sandbox profile ignored")
    endpoints.extend(profile.raw_egress)
    return list(dict.fromkeys(endpoints))


# Host the OTel collector rule uses (see sandbox._apply_policy).
_OTEL_COLLECTOR_HOST = "host.openshell.internal"

# Hosts that stay bound to the agent binaries in every phase: the LLM and auth
# endpoints of every auth mode (the provider injects credentials there) and
# the OTel collector.
_AGENT_ONLY_HOSTS = frozenset(
    {ep.split(":", 1)[0].lower() for eps in AUTH_ENDPOINTS.values() for ep in eps}
    | {_OTEL_COLLECTOR_HOST}
)


def _agent_only(endpoint: str) -> bool:
    """Whether *endpoint* must never be opened to the setup shim.

    True when it carries a credential option, or when its host (or wildcard)
    overlaps an agent-only host in either direction, so ``*.googleapis.com``
    counts as ``oauth2.googleapis.com``.
    """
    parts = endpoint.split(":")
    options = parts[5].split(",") if len(parts) > 5 else []
    if any(option in CREDENTIAL_ENDPOINT_OPTIONS for option in options):
        return True
    host = parts[0].lower()
    return any(fnmatchcase(host, h) or fnmatchcase(h, host) for h in _AGENT_ONLY_HOSTS)


def phase_endpoints(profile: SandboxProfile, phase: str) -> list[str]:
    """Return the endpoints *profile* opens to the setup shim in *phase*.

    *phase* is ``"setup"`` or ``"validate"``. The result holds the endpoints
    of the profile's presets open in that phase, then the profile's raw
    egress, de-duplicated. It never includes the built-in defaults, the
    auth (LLM) endpoints or the OTel collector: those stay bound to the agent
    binaries only. A raw endpoint whose host overlaps an auth endpoint or the
    collector, or that carries a credential option, is left out as well;
    only their count is logged.
    """
    if phase not in _SHIM_PHASES:
        raise ValueError(f"phase must be one of {', '.join(_SHIM_PHASES)}, not {phase!r}")
    endpoints = _profile_endpoints(profile, phase)
    kept = [ep for ep in endpoints if not _agent_only(ep)]
    dropped = len(endpoints) - len(kept)
    if dropped:
        log.info(
            f"WARNING: {dropped} egress endpoint(s) not opened in the {phase} phase: "
            "LLM, auth and credential endpoints stay with the agent"
        )
    return kept


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


def resolve_endpoints(flag_path=None, workdir=".", auth_mode=None, profile=None):
    """Resolve the endpoint list to use for policy update.

    Merges the built-in defaults and endpoints required by *auth_mode* with
    extra endpoints from, in priority order:

    1. Explicit ``--policy`` flag path
    2. ``.agentic-ci/openshell-policy.yml`` in *workdir*

    With a sandbox *profile*, the repo policy file is ignored (egress then
    comes from reviewed configuration only) and the endpoints of the
    profile's presets open in the ``agent`` phase plus its raw egress are
    added after the defaults and auth endpoints. An explicit ``--policy``
    flag still applies.

    Returns a list of endpoint strings for ``openshell policy update --add-endpoint``.
    """
    extra = []
    source = "built-in default"
    repo_path = os.path.join(workdir, REPO_POLICY_PATH)

    if flag_path and os.path.isfile(flag_path):
        extra = _load_endpoints_from_file(flag_path)
        source = f"--policy flag ({os.path.abspath(flag_path)})"
        if profile is not None:
            source += " and sandbox profile"
    elif profile is not None:
        source = "sandbox profile"
        if os.path.isfile(repo_path):
            source += " (repo policy file ignored)"
    elif os.path.isfile(repo_path):
        extra = _load_endpoints_from_file(repo_path)
        source = f"repo ({os.path.abspath(repo_path)})"

    print(f"  Policy source: {source}", flush=True)

    endpoints = list(DEFAULT_ENDPOINTS)
    endpoints.extend(AUTH_ENDPOINTS.get(auth_mode, []))
    if profile is not None:
        extra = _profile_endpoints(profile, "agent") + extra
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
                ep["allow_uninspected_credentials"] = True
                changed = True

    return patched if changed else None
