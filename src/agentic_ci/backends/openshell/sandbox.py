"""OpenShell sandbox lifecycle management."""

import subprocess

SANDBOX_NAME = "ci"


def exists():
    """Check if the sandbox already exists."""
    result = subprocess.run(
        ["openshell", "sandbox", "get", SANDBOX_NAME],
        capture_output=True,
    )
    return result.returncode == 0


def create(image=None, policy_path=None):
    """Create a persistent sandbox."""
    args = [
        "openshell",
        "sandbox",
        "create",
        "--name",
        SANDBOX_NAME,
        "--no-tty",
        "--no-auto-providers",
    ]
    if image:
        args.extend(["--from", image])
    if policy_path:
        args.extend(["--policy", policy_path])
    args.extend(["--", "true"])
    subprocess.run(args, check=True)


def upload(local_path):
    """Upload a local path into the sandbox."""
    subprocess.run(
        ["openshell", "sandbox", "upload", "--no-git-ignore", SANDBOX_NAME, local_path],
        check=True,
    )


def exec_cmd(cmd):
    """Run a command inside the sandbox. Returns the CompletedProcess."""
    return subprocess.run(
        ["openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty", "--"] + cmd,
        check=True,
    )


def exec_cmd_streaming(cmd):
    """Run a command inside the sandbox with stdout piped. Returns a Popen."""
    return subprocess.Popen(
        ["openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty", "--"] + cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def exec_cmd_to_file(cmd, stdout_fh, stderr_fh=None, timeout=None):
    """Run a command inside the sandbox with stdout/stderr redirected to file handles.

    Returns the exit code.
    """
    proc = subprocess.Popen(
        ["openshell", "sandbox", "exec", "--name", SANDBOX_NAME, "--no-tty", "--"] + cmd,
        stdout=stdout_fh,
        stderr=stderr_fh if stderr_fh is not None else subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return proc.returncode


def delete():
    """Delete the sandbox."""
    subprocess.run(
        ["openshell", "sandbox", "delete", SANDBOX_NAME],
        check=True,
    )
