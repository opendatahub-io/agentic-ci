"""OpenShell gateway lifecycle management."""

import os
import subprocess
import time

GATEWAY_PORT = 8222
HEALTH_PORT = 8223


def is_running():
    """Check if the OpenShell gateway is healthy."""
    try:
        result = subprocess.run(
            ["curl", "-sf", f"http://127.0.0.1:{HEALTH_PORT}/healthz"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def start():
    """Start the OpenShell gateway with the podman driver.

    Starts the podman API socket, generates a handshake secret, and launches
    openshell-gateway in the background. Blocks until the health endpoint responds.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.makedirs(f"{xdg}/podman", exist_ok=True)

    subprocess.Popen(
        ["podman", "system", "service", "--time=0", f"unix://{xdg}/podman/podman.sock"],
    )
    time.sleep(1)

    secret = os.urandom(16).hex()
    env = {
        **os.environ,
        "OPENSHELL_SSH_HANDSHAKE_SECRET": secret,
        "OPENSHELL_SUPERVISOR_IMAGE": os.environ.get(
            "OPENSHELL_SUPERVISOR_IMAGE", "openshell/supervisor:dev"
        ),
    }

    subprocess.Popen(
        [
            "openshell-gateway",
            "--bind-address",
            "0.0.0.0",
            "--port",
            str(GATEWAY_PORT),
            "--health-port",
            str(HEALTH_PORT),
            "--drivers",
            "podman",
            "--disable-tls",
            "--disable-gateway-auth",
            "--db-url",
            "sqlite::memory:",
            "--log-level",
            "info",
        ],
        stdout=open("/tmp/openshell-gateway.log", "w"),
        stderr=subprocess.STDOUT,
        env=env,
    )

    for _ in range(30):
        if is_running():
            return
        time.sleep(2)

    raise RuntimeError("Gateway did not become healthy within 60s")
