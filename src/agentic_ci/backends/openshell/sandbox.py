"""OpenShell sandbox lifecycle management."""

import subprocess

from agentic_ci import log
from agentic_ci.backends.openshell.policy import resolve_endpoints
from agentic_ci.backends.openshell.provider import PROVIDER_NAME

SANDBOX_NAME = "ci"


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


def create(image=None, policy_path=None, otel_port=None, workdir="."):
    """Create a persistent sandbox with the CI provider attached.

    The sandbox is created first, then the network policy is applied
    via ``openshell policy update --wait`` to ensure the supervisor
    has compiled and activated the rules before the agent starts.
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
    if image:
        args.extend(["--from", image])
    args.extend(["--", "true"])
    _run(args, check=True)

    _apply_policy(policy_path, otel_port=otel_port, workdir=workdir)


def _apply_policy(policy_path, otel_port=None, workdir="."):
    """Apply network policy endpoints and wait for activation.

    Uses ``openshell policy update --wait`` which blocks until the
    supervisor has compiled and loaded the new policy revision.
    """
    endpoints = resolve_endpoints(policy_path, workdir=workdir)
    if otel_port:
        endpoints.append(f"host.openshell.internal:{otel_port}:read-write")
    if not endpoints:
        return

    args = [
        "openshell",
        "policy",
        "update",
        "--wait",
        "--binary",
        "/usr/local/bin/claude",
        "--binary",
        "/usr/bin/opencode",
    ]
    for ep in endpoints:
        args.extend(["--add-endpoint", ep])
    args.append(SANDBOX_NAME)
    _run(args, check=True)


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
