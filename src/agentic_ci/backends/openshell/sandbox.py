"""OpenShell sandbox lifecycle management."""

import os
import subprocess
import tempfile

from agentic_ci import log
from agentic_ci.backends.openshell.policy import (
    generate_policy_yaml,
    resolve_endpoints,
)
from agentic_ci.backends.openshell.provider import PROVIDER_NAME

SANDBOX_NAME = "ci"


def _run(args, **kwargs):
    """Run an openshell command with logging.

    Stdin is closed by default to work around OpenShell CLI bug #828:
    the CLI hangs indefinitely when stdin is inherited from a non-closing
    source because the gRPC client never sends END_STREAM.
    """
    kwargs.setdefault("stdin", subprocess.DEVNULL)
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
    tls_skip_hosts=None,
    binaries=None,
):
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
    if approval_mode:
        args.extend(["--approval-mode", approval_mode])
    if image:
        args.extend(["--from", image])
    args.extend(["--", "true"])
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
        tls_skip_hosts=tls_skip_hosts,
        binaries=binaries,
    )


def _apply_policy(policy_path, otel_port=None, workdir=".", tls_skip_hosts=None, binaries=None):
    """Apply network policy endpoints and wait for activation.

    If the harness declares ``tls_skip_hosts``, a full YAML policy is
    generated and applied via ``openshell policy set`` so that those
    endpoints can use ``tls: skip`` (the ``--add-endpoint`` shorthand
    does not support it).  Otherwise ``openshell policy update
    --add-endpoint`` is used.
    """
    endpoints = resolve_endpoints(policy_path, workdir=workdir, tls_skip_hosts=tls_skip_hosts)
    if not endpoints:
        return

    binaries = binaries or []

    if tls_skip_hosts:
        # The harness provides structured tuples (host, port, access); the
        # policy generator needs a set of plain hostnames for tls:skip matching.
        tls_host_names = [h for h, _, _ in tls_skip_hosts]
        _apply_policy_yaml(endpoints, binaries, tls_skip_hosts=tls_host_names, otel_port=otel_port)
    else:
        _apply_policy_update(endpoints, binaries, otel_port=otel_port)


def _apply_policy_update(endpoints, binaries, otel_port=None):
    """Incremental policy update via ``openshell policy update``."""
    if otel_port:
        endpoints.append(f"host.openshell.internal:{otel_port}:read-write")

    args = ["openshell", "policy", "update", "--wait"]
    for binary in binaries:
        args.extend(["--binary", binary])
    for ep in endpoints:
        args.extend(["--add-endpoint", ep])
    args.append(SANDBOX_NAME)
    _run(args, check=True)


def _apply_policy_yaml(endpoints, binaries, tls_skip_hosts=None, otel_port=None):
    """Full policy replacement via ``openshell policy set``.

    Required when a harness declares ``tls_skip_hosts`` because ``tls: skip``
    cannot be expressed through the ``--add-endpoint`` CLI shorthand.
    """
    policy_yaml = generate_policy_yaml(
        endpoints,
        binaries,
        tls_skip_hosts=tls_skip_hosts,
        otel_port=otel_port,
    )
    fd, path = tempfile.mkstemp(prefix="openshell-policy-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(policy_yaml)
        _run(
            ["openshell", "policy", "set", SANDBOX_NAME, "--policy", path, "--wait"],
            check=True,
        )
    finally:
        os.unlink(path)


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
    """Run a command inside the sandbox with stdout piped. Returns a Popen.

    Stdin is closed to work around OpenShell CLI bug #828.
    """
    args = ["openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty", "--"] + cmd
    log.detail("exec", " ".join(args))
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def delete():
    """Delete the sandbox."""
    _run(
        ["openshell", "sandbox", "delete", SANDBOX_NAME],
        check=True,
    )
