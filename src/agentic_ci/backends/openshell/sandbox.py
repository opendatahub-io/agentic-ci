"""OpenShell sandbox lifecycle management."""

import json
import os
import subprocess
import tempfile

import yaml

from agentic_ci import log
from agentic_ci.backends.openshell.policy import build_credential_binding_patch, resolve_endpoints
from agentic_ci.backends.openshell.provider import PROVIDER_NAME

SANDBOX_NAME = "ci"

AGENT_BINARY_PATHS = (
    "/usr/local/bin/claude",
    "/usr/local/sbin/claude",
    "/usr/local/bin/opencode",
    "/usr/local/sbin/opencode",
    "/usr/local/bin/codex",
    "/usr/local/sbin/codex",
)


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
    image=None,
    policy_path=None,
    otel_port=None,
    workdir=".",
    approval_mode=None,
    auth_mode=None,
    memory: str | None = None,
    cpu: str | None = None,
    gpu: int | None = None,
) -> None:
    """Create a persistent sandbox with the CI provider attached.

    The sandbox is created first, then the network policy is applied
    via ``openshell policy update --wait`` to ensure the supervisor
    has compiled and activated the rules before the agent starts.

    ``memory``, ``cpu`` and ``gpu`` size the sandbox. All three default to
    ``None``, which leaves OpenShell's own defaults in place -- notably a
    memory ceiling of roughly 4Gi, which applies no matter how large the CI
    runner is. An agent that exceeds it is OOM-killed by the cgroup, and from
    inside the sandbox that looks like the sandbox simply vanishing: the
    supervisor's log stops mid-line and the next command reports
    ``sandbox is not ready``.

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
    """
    args = [
        "openshell",
        "sandbox",
        "create",
        "--name",
        SANDBOX_NAME,
        "--no-tty",
        "--no-auto-providers",
        "--provider",
        PROVIDER_NAME,
    ]
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
    )


def _apply_policy(policy_path, otel_port=None, workdir=".", auth_mode=None):
    """Apply network policy endpoints and wait for activation.

    Two-step process:
    1. ``openshell policy update`` to add endpoints incrementally (this
       preserves filesystem_policy and other static fields).
    2. ``openshell policy get --base`` + merge credential_binding + ``openshell
       policy set`` to add credential_binding.provider on GCP endpoints.
       The google-cloud provider profile is endpointless, so the gateway
       withholds credentials unless the sandbox policy explicitly binds them.
    """
    endpoints = resolve_endpoints(policy_path, workdir=workdir, auth_mode=auth_mode)
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
