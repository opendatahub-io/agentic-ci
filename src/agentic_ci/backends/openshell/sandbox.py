"""OpenShell sandbox lifecycle management."""

import copy
import json
import os
import subprocess
import tempfile
from collections.abc import Iterable

import yaml

from agentic_ci import log
from agentic_ci.backends.openshell.policy import (
    EGRESS_PHASES,
    build_credential_binding_patch,
    resolve_endpoints,
)
from agentic_ci.backends.openshell.provider import PROVIDER_NAME, requires_provider
from agentic_ci.sandbox_profile import SandboxProfile, is_raw_endpoint

SANDBOX_NAME = "ci"

AGENT_BINARY_PATHS = (
    "/usr/local/bin/claude",
    "/usr/local/sbin/claude",
    "/usr/local/bin/opencode",
    "/usr/local/sbin/opencode",
    "/usr/local/bin/codex",
    "/usr/local/sbin/codex",
)

# Setup and validate steps run under this shim, shipped in the sandbox images.
# OpenShell matches a connection to rules by the caller's executable and its
# parent chain, so rules bound to the shim apply to a step and everything it
# starts, and to nothing else. The shim forks and waits instead of exec-ing,
# so it stays in the parent chain.
SANDBOX_SETUP_SHIM = "/usr/local/bin/agentic-ci-sandbox-setup"

# Rules the phase switch adds are named PHASE_RULE_PREFIX + index. Any rule
# with this prefix is replaced on every switch.
PHASE_RULE_PREFIX = "agentic_ci_phase_"

# Rules OpenShell composes for an attached provider. They are not part of
# ``policy get --base`` and the server refuses them in ``policy set``.
_PROVIDER_RULE_PREFIX = "_provider_"

# While the setup shim's egress is open (the setup and validate phases), every
# agent binary path in a user rule is rewritten to this prefix plus the path,
# so the rule stays in the policy with its credential bindings but matches no
# process: a step cannot reach agent egress by running an agent binary under
# the shim. The agent phase strips the prefix again. Nothing can be created
# under /proc, so no executable can ever have a parked path.
PARKED_BINARY_PREFIX = "/proc/agentic-ci-parked"

_ALLOWED_IP_OPTION = "allowed-ip="


def _run(args, **kwargs):
    """Run an openshell command with logging."""
    log.detail("exec", " ".join(args))
    return subprocess.run(args, **kwargs)


def exists():
    """Check if the sandbox already exists."""
    result = _run(
        ["openshell", "sandbox", "get", SANDBOX_NAME],
        capture_output=True,
    )
    return result.returncode == 0


def create(
    image: str | None = None,
    policy_path: str | None = None,
    otel_port: int | None = None,
    workdir: str = ".",
    approval_mode: str | None = None,
    auth_mode: str | None = None,
    memory: str | None = None,
    cpu: str | None = None,
    gpu: int | None = None,
    profile: SandboxProfile | None = None,
) -> None:
    """Create a persistent sandbox with the CI provider attached, if *auth_mode* uses one.

    The sandbox is created first, then the network policy is applied
    via ``openshell policy update --wait`` to ensure the supervisor
    has compiled and activated the rules before the agent starts.

    ``memory``, ``cpu`` and ``gpu`` size the sandbox. All three default to
    ``None``, which passes no resource flag and leaves the effective limits to
    the compute driver and host configuration. An agent that exceeds a memory
    limit is OOM-killed by the cgroup, and from inside the sandbox that looks
    like the sandbox simply vanishing: the supervisor's log stops mid-line and
    the next command reports ``sandbox is not ready``.

    Args:
        image: Sandbox image to create from.
        policy_path: Path to a network policy file to merge in.
        otel_port: Port for the OTEL collector on the host.
        workdir: Directory the policy file is resolved relative to.
        approval_mode: Enables agent policy proposals when set.
        memory: Memory limit, e.g. ``"8Gi"``. None uses OpenShell's default.
        cpu: CPU limit, e.g. ``"4"`` or ``"2.5"``. None uses OpenShell's default.
        gpu: GPU count to request, e.g. ``1``. None requests no GPU, which
            means an accelerator on the host is not visible to the agent even
            when the container running this can see it.
        profile: Sandbox profile whose agent-phase egress is added to the
            policy. None keeps the policy exactly as without profiles.
    """
    args = [
        "openshell",
        "sandbox",
        "create",
        "--name",
        SANDBOX_NAME,
        "--no-tty",
        "--no-auto-providers",
    ]
    if requires_provider(auth_mode):
        args.extend(["--provider", PROVIDER_NAME])
    if approval_mode:
        args.extend(["--approval-mode", approval_mode])
    if image:
        args.extend(["--from", image])
    if memory:
        args.extend(["--memory", str(memory)])
    if cpu:
        args.extend(["--cpu", str(cpu)])
    if gpu:
        args.extend(["--gpu", str(gpu)])
    # The trailing argv becomes the sandbox's canonical main process.
    # Use a persistent process so the supervisor stays alive to accept
    # policy updates; --detach returns control to the caller immediately.
    args.extend(["--detach", "--", "sleep", "infinity"])
    _run(args, check=True)

    if approval_mode:
        _run(
            [
                "openshell",
                "settings",
                "set",
                SANDBOX_NAME,
                "--key",
                "agent_policy_proposals_enabled",
                "--value",
                "true",
            ],
            check=True,
        )

    _apply_policy(
        policy_path,
        otel_port=otel_port,
        workdir=workdir,
        auth_mode=auth_mode,
        profile=profile,
    )


def _apply_policy(policy_path, otel_port=None, workdir=".", auth_mode=None, profile=None):
    """Apply network policy endpoints and wait for activation.

    Two-step process:
    1. ``openshell policy update`` to add endpoints incrementally (this
       preserves filesystem_policy and other static fields).
    2. ``openshell policy get --base`` + merge credential_binding + ``openshell
       policy set`` to add credential_binding.provider on GCP endpoints.
       The google-cloud provider profile is endpointless, so the gateway
       withholds credentials unless the sandbox policy explicitly binds them.
       Skipped for auth modes with no provider, which has nothing to bind.
    """
    endpoints = resolve_endpoints(
        policy_path, workdir=workdir, auth_mode=auth_mode, profile=profile
    )
    if otel_port:
        endpoints.append(f"host.openshell.internal:{otel_port}:read-write")
    if not endpoints:
        return

    args = [
        "openshell",
        "policy",
        "update",
        "--wait",
    ]
    for binary_path in AGENT_BINARY_PATHS:
        args.extend(["--binary", binary_path])
    for ep in endpoints:
        args.extend(["--add-endpoint", ep])
    args.append(SANDBOX_NAME)
    _run(args, check=True)

    if requires_provider(auth_mode):
        _apply_credential_bindings()


def _apply_credential_bindings():
    """Patch the active policy with credential_binding on GCP endpoints.

    Reads the current base policy, adds credential_binding.provider to
    matching GCP endpoints, then sets the merged policy back.
    """
    result = _run(
        ["openshell", "policy", "get", "--base", "-o", "json", SANDBOX_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return

    try:
        policy = json.loads(result.stdout)
    except json.JSONDecodeError:
        return

    patched = build_credential_binding_patch(policy)
    if patched is None:
        return

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(patched, f, default_flow_style=False)
        policy_file = f.name

    try:
        _run(
            ["openshell", "policy", "set", "--wait", "--policy", policy_file, SANDBOX_NAME],
            check=True,
        )
    finally:
        os.unlink(policy_file)


def _endpoint_to_policy(spec, index):
    """Convert one ``host:port:access[:protocol[:enforcement[:options]]]`` string.

    Returns the endpoint mapping a policy file uses (the shape
    ``openshell policy get -o json`` prints). The grammar is the one a central
    profile's raw egress must follow (:func:`agentic_ci.sandbox_profile.is_raw_endpoint`).
    Only ``allowed-ip=`` options are accepted: the credential options are
    never given to a setup-shim rule. Raises ``ValueError`` naming only the
    endpoint's position, never its text.
    """
    if not is_raw_endpoint(spec):
        raise ValueError(
            f"phase endpoint {index} is not host:port:access[:protocol[:enforcement[:options]]]"
        )
    host, port, access, protocol, enforcement, options = (spec.split(":") + [""] * 6)[:6]
    endpoint = {"host": host, "port": int(port), "access": access}
    if protocol:
        endpoint["protocol"] = protocol
    if enforcement:
        endpoint["enforcement"] = enforcement
    allowed_ips = []
    for option in options.split(",") if options else ():
        if not option.startswith(_ALLOWED_IP_OPTION):
            raise ValueError(
                f"phase endpoint {index} has a credential option, "
                "which a setup-shim rule never gets"
            )
        value = option[len(_ALLOWED_IP_OPTION) :]
        if value not in allowed_ips:
            allowed_ips.append(value)
    if allowed_ips:
        endpoint["allowed_ips"] = allowed_ips
    return endpoint


def _rebind(rule, park):
    """Return *rule* with its ``binaries`` adjusted for a phase, or None to drop it.

    Removes :data:`SANDBOX_SETUP_SHIM`, dropping the rule only when the shim
    was its only binary. With *park*, every agent binary path gets
    :data:`PARKED_BINARY_PREFIX`; without it, parked paths get their original
    path back (without duplicating one that is already there). An empty
    ``binaries`` list means any binary to OpenShell, so no rule is ever turned
    into one; a rule that already had no binaries is left as it is.
    """
    binaries = rule.get("binaries") if isinstance(rule, dict) else None
    if not isinstance(binaries, list) or not binaries:
        return rule
    kept = []
    for binary in binaries:
        path = binary.get("path") if isinstance(binary, dict) else None
        if path == SANDBOX_SETUP_SHIM:
            continue
        if isinstance(path, str):
            if park and path in AGENT_BINARY_PATHS:
                binary = {**binary, "path": PARKED_BINARY_PREFIX + path}
            elif not park and path.startswith(PARKED_BINARY_PREFIX + "/"):
                binary = {**binary, "path": path[len(PARKED_BINARY_PREFIX) :]}
            if any(isinstance(b, dict) and b.get("path") == binary["path"] for b in kept):
                continue
        kept.append(binary)
    if not kept:
        return None
    rule["binaries"] = kept
    return rule


def build_phase_policy(base_get_output, endpoints: Iterable[str], *, park_agent=False):
    """Build the policy for an egress phase from ``openshell policy get --base -o json``.

    Takes the ``.policy`` object, deep-copies it, and in ``network_policies``:

    1. drops every rule named with :data:`PHASE_RULE_PREFIX` (the previous
       phase's rules) or ``_provider_`` (composed by the server);
    2. removes :data:`SANDBOX_SETUP_SHIM` from every other rule's
       ``binaries``, dropping a rule only when the shim was its only binary;
    3. with *park_agent* (the setup and validate phases), parks every agent
       binary path under :data:`PARKED_BINARY_PREFIX` so no process matches
       the agent's rules while the shim's egress is open; without it (the
       agent phase), restores the parked paths;
    4. adds one rule per endpoint, in order and de-duplicated, named
       ``PHASE_RULE_PREFIX + index`` and bound to the shim only.

    One rule per endpoint mirrors the rules ``openshell policy update``
    creates and the shape the September 2026 spike verified, and keeps each
    endpoint's options independent. Every other field (``version``,
    ``filesystem_policy``, ``landlock``, ``process``, rule names and endpoints,
    and the credential bindings on user rules) is returned unchanged:
    ``openshell policy set`` needs the whole object and refuses to drop the
    static fields of a live sandbox.

    Returns the raw policy dict for ``openshell policy set``. Raises
    ``ValueError`` when ``.policy`` or its ``network_policies`` is missing or
    an endpoint is malformed; the message never includes policy content.
    """
    raw_policy = base_get_output.get("policy") if isinstance(base_get_output, dict) else None
    if not isinstance(raw_policy, dict):
        raise ValueError("policy get output has no 'policy' object")
    if not isinstance(raw_policy.get("network_policies"), dict):
        raise ValueError("policy get output has no 'network_policies' mapping")

    new_rules = [
        _endpoint_to_policy(spec, index) for index, spec in enumerate(dict.fromkeys(endpoints))
    ]

    policy = copy.deepcopy(raw_policy)
    rules = {}
    for name, rule in policy["network_policies"].items():
        if str(name).startswith((PHASE_RULE_PREFIX, _PROVIDER_RULE_PREFIX)):
            continue
        kept = _rebind(rule, park_agent)
        if kept is not None:
            rules[name] = kept
    for index, endpoint in enumerate(new_rules):
        name = f"{PHASE_RULE_PREFIX}{index}"
        rules[name] = {
            "name": name,
            "endpoints": [endpoint],
            "binaries": [{"path": SANDBOX_SETUP_SHIM}],
        }
    policy["network_policies"] = rules
    return policy


def _log_stderr(result):
    stderr = (result.stderr or "").strip()
    if stderr:
        log.detail("openshell stderr", stderr)


def apply_phase_policy(phase, endpoints):
    """Switch the sandbox's setup-shim egress to *phase*.

    Reads the base policy, builds the phase policy with
    :func:`build_phase_policy` and applies the whole object with
    ``openshell policy set --wait``. ``openshell policy update`` is never
    used here: it folds an endpoint whose host overlaps an existing rule into
    that rule, where the shim could not be removed again.

    *phase* is one of ``setup``, ``validate`` or ``agent``. ``setup`` and
    ``validate`` also park the agent's rules (see :data:`PARKED_BINARY_PREFIX`),
    so running an agent binary under the shim gains nothing. ``agent`` strips
    every shim-bound rule, restores the agent's rules and takes no endpoints.
    Any policy change closes every open proxied connection in the sandbox,
    so switch only while nothing runs there. Nothing is applied when the
    policy would not change, so switching to the phase already in effect is
    free.

    Raises ``RuntimeError`` when an ``openshell`` command fails or prints
    unusable output. The message names the step and the failure class only;
    stderr goes to the job log.
    """
    if phase not in EGRESS_PHASES:
        raise ValueError(f"phase must be one of {', '.join(EGRESS_PHASES)}, not {phase!r}")
    endpoints = list(endpoints)
    if phase == "agent" and endpoints:
        raise ValueError("the agent phase opens no setup-shim endpoints")

    result = _run(
        ["openshell", "policy", "get", "--base", "-o", "json", SANDBOX_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _log_stderr(result)
        raise RuntimeError(
            f"Could not switch to the {phase} egress phase: openshell policy get "
            f"exited with status {result.returncode}; see the job log"
        )
    try:
        current = json.loads(result.stdout)
        policy = build_phase_policy(current, endpoints, park_agent=phase != "agent")
    except ValueError as exc:
        raise RuntimeError(
            f"Could not switch to the {phase} egress phase: unusable policy get output "
            f"({type(exc).__name__}); see the job log"
        ) from exc

    if policy == current["policy"]:
        log.info(f"Egress phase {phase}: policy unchanged")
        return

    fd, policy_file = tempfile.mkstemp(prefix="agentic-ci-phase-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump(policy, fh, default_flow_style=False)
        result = _run(
            ["openshell", "policy", "set", "--wait", "--policy", policy_file, SANDBOX_NAME],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(policy_file)
    if result.returncode != 0:
        _log_stderr(result)
        raise RuntimeError(
            f"Could not switch to the {phase} egress phase: openshell policy set "
            f"exited with status {result.returncode}; see the job log"
        )
    opened = len(dict.fromkeys(endpoints))
    log.info(f"Egress phase {phase}: {opened} setup-shim endpoint(s) open")


def upload(local_path):
    """Upload a local path into the sandbox."""
    _run(
        ["openshell", "sandbox", "upload", "--no-git-ignore", SANDBOX_NAME, local_path],
        check=True,
    )


def download(sandbox_path, local_dest):
    """Download a path from the sandbox to a local destination."""
    _run(
        ["openshell", "sandbox", "download", SANDBOX_NAME, sandbox_path, local_dest],
        check=True,
    )


def exec_cmd(cmd):
    """Run a command inside the sandbox. Returns the CompletedProcess."""
    return _run(
        ["openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty", "--"] + cmd,
        check=True,
    )


def exec_cmd_streaming(cmd):
    """Run a command inside the sandbox with stdout piped. Returns a Popen."""
    args = ["openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty", "--"] + cmd
    log.detail("exec", " ".join(args))
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def delete():
    """Delete the sandbox."""
    _run(
        ["openshell", "sandbox", "delete", SANDBOX_NAME],
        check=True,
    )
