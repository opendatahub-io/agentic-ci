"""OpenShell gateway lifecycle management."""

import os
import re
import signal
import subprocess
import tempfile
import time

import tenacity

from agentic_ci import log

GATEWAY_PORT = 17670

_GATEWAY_TOML = """\
[openshell]
version = 1

[openshell.gateway]
# 0.0.0.0 is required: the sandbox supervisor connects to the gateway
# via the container bridge network (host.containers.internal), which is
# not reachable on 127.0.0.1. TLS+mTLS is enabled, so unauthenticated
# access is rejected.
bind_address = "0.0.0.0:{port}"
compute_drivers = ["podman"]
"""


def is_running():
    """Check if the OpenShell gateway is registered and healthy."""
    try:
        cmd = ["openshell", "status"]
        log.detail("exec", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
        if result.returncode != 0:
            return False
        return "No gateway configured" not in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def start():
    """Start the OpenShell gateway with the podman driver.

    Starts the podman API socket, generates TLS certificates for sandbox
    JWT auth, writes a gateway config, launches openshell-gateway in the
    background, registers it with the CLI, and blocks until the health
    endpoint responds.

    If any step fails after processes have been spawned, cleanup is
    performed automatically to avoid orphaned processes.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sock = f"{xdg}/podman/podman.sock"
    os.makedirs(f"{xdg}/podman", exist_ok=True)

    subprocess.Popen(
        ["podman", "system", "service", "--time=0", f"unix://{sock}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        _wait_for_socket(sock)
        _write_config()
        _generate_certs()

        supervisor_image = os.environ.get("OPENSHELL_SUPERVISOR_IMAGE")
        if supervisor_image:
            print(f"  Supervisor image: {supervisor_image}", flush=True)

        state_dir = os.path.expanduser("~/.local/state/openshell")
        os.makedirs(state_dir, exist_ok=True)
        log_file = tempfile.NamedTemporaryFile(
            mode="w",
            dir=state_dir,
            prefix="gateway-",
            suffix=".log",
            delete=False,
        )
        subprocess.Popen(
            [
                "openshell-gateway",
                "--db-url",
                "sqlite::memory:",
                "--log-level",
                "info",
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        log_file.close()

        _register()

        for _ in range(30):
            if is_running():
                return
            time.sleep(2)

        raise RuntimeError("Gateway did not become healthy within 60s")

    except Exception:
        stop()
        raise


def stop():
    """Terminate the gateway and podman service processes.

    Deregisters the gateway from the CLI first, then discovers and kills
    processes by port and socket rather than requiring stored handles, so
    this works across process boundaries (e.g. a separate
    ``agentic-ci stop`` invocation).
    """
    # remove only clears CLI metadata, it does not stop the process
    try:
        cmd = ["openshell", "gateway", "remove", "ci"]
        log.detail("exec", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, timeout=5, text=True)
        if result.returncode != 0 and result.stderr:
            print(f"  gateway remove: {result.stderr.strip()}", flush=True)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    _kill_gateway()
    _kill_podman_service()


def _kill_gateway():
    """Kill the gateway process listening on GATEWAY_PORT."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if str(GATEWAY_PORT) not in line:
                continue
            match = re.search(r"pid=(\d+)", line)
            if match:
                pid = int(match.group(1))
                os.kill(pid, signal.SIGTERM)
                _wait_for_pid(pid, timeout=10)
                return
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _kill_podman_service():
    """Kill the podman system service started by this module.

    Matches the full command including our socket path so we don't
    terminate unrelated podman services on the same host.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sock = f"unix://{xdg}/podman/podman.sock"
    try:
        subprocess.run(
            ["pkill", "-f", f"podman system service --time=0 {sock}"],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _wait_for_pid(pid, timeout=10):
    """Wait for a process to exit, escalating to SIGKILL if needed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _wait_for_socket(path, timeout=15):
    """Poll until the podman API socket exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Podman socket did not appear at {path} within {timeout}s")


def _write_config():
    """Write the gateway TOML config, updating it if the content changed."""
    config_dir = os.path.expanduser("~/.config/openshell")
    config_path = os.path.join(config_dir, "gateway.toml")
    rendered = _GATEWAY_TOML.format(port=GATEWAY_PORT)

    supervisor_image = os.environ.get("OPENSHELL_SUPERVISOR_IMAGE")
    if supervisor_image:
        rendered += f'\n[openshell.drivers.podman]\nsupervisor_image = "{supervisor_image}"\n'

    if os.path.isfile(config_path):
        with open(config_path) as f:
            if f.read() == rendered:
                return
    os.makedirs(config_dir, exist_ok=True)
    with open(config_path, "w") as f:
        f.write(rendered)


def _generate_certs():
    """Generate TLS certificates for the gateway."""
    tls_dir = os.path.expanduser("~/.local/state/openshell/tls")
    os.makedirs(tls_dir, exist_ok=True)
    env = {**os.environ, "OPENSHELL_LOCAL_TLS_DIR": tls_dir}
    cmd = [
        "openshell-gateway",
        "generate-certs",
        "--output-dir",
        tls_dir,
        "--server-san",
        "host.openshell.internal",
    ]
    log.detail("exec", " ".join(cmd))
    subprocess.run(cmd, check=True, env=env)


@tenacity.retry(
    wait=tenacity.wait_fixed(2),
    stop=tenacity.stop_after_attempt(10),
    retry=tenacity.retry_if_exception_type(subprocess.CalledProcessError),
    reraise=True,
)
def _register():
    """Register the local gateway with the OpenShell CLI.

    Retries because the gateway process may not be listening yet when
    registration is first attempted.
    """
    cmd = [
        "openshell",
        "gateway",
        "add",
        f"https://localhost:{GATEWAY_PORT}",
        "--local",
        "--name",
        "ci",
    ]
    log.detail("exec", " ".join(cmd))
    subprocess.run(cmd, check=True, timeout=30)
