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
_GATEWAY_DB_PATH: str | None = None

# Oldest OpenShell release agentic-ci drives: gateway.toml schema version 2,
# `sandbox provider attach|detach --wait`, provider profiles with
# `refresh_before` durations and the trusted host gateway default that lets
# policy DNS map host.openshell.internal. Older runtimes fail late and
# opaquely (a gateway that never gets healthy, a CLI usage error).
MIN_OPENSHELL_VERSION = (0, 1, 2)
_VERSION_BINARIES = ("openshell", "openshell-gateway")
_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)")

# host_gateway_ip is deliberately not set. OpenShell v0.1.x defaults the
# podman driver's trusted host gateway to 127.0.0.1, which lets policy DNS
# map host.openshell.internal for the OTel collector rule. The v0.0.116
# rhaiv.8 to rhaiv.11 drivers sent no trusted gateway at all, and every
# lookup of that name was denied (policy_dns_trusted_gateway_unavailable).
_GATEWAY_TOML = """\
[openshell]
# Schema version 2 is required since OpenShell v0.0.116-rhaiv.8 (v0.1.x
# included); the gateway rejects version 1 files outright.
version = 2

[openshell.gateway]
# 0.0.0.0 is required: the sandbox supervisor connects to the gateway
# via the container bridge network (host.containers.internal), which is
# not reachable on 127.0.0.1. TLS+mTLS is enabled, so unauthenticated
# access is rejected.
bind_address = "0.0.0.0:{port}"
# Singular selector. Schema version 2 rejects the legacy plural list form
# as an unknown field.
compute_driver = "podman"
"""

# (label, env var, gateway.toml key) for the OpenShell driver images. The
# gateway does not read these env vars itself; agentic-ci renders them into
# the ``[openshell.drivers.podman]`` section of gateway.toml. Since the
# supervisor/sandbox split (NVIDIA/OpenShell#2942) the podman driver needs
# two images per sandbox: the supervisor runs in its own container and the
# sandbox runtime image supplies the static ``openshell-sandbox`` binary
# that is mounted read-only into the workload container.
_DRIVER_IMAGES = (
    ("Supervisor image", "OPENSHELL_SUPERVISOR_IMAGE", "supervisor_image"),
    ("Sandbox runtime image", "OPENSHELL_SANDBOX_RUNTIME_IMAGE", "sandbox_runtime_image"),
)


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


def _binary_version(binary):
    """Return *binary*'s ``--version`` as a (major, minor, patch) tuple, or None."""
    cmd = [binary, "--version"]
    log.detail("exec", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.detail("version", f"{binary}: {exc}")
        return None
    log.detail("version", f"{binary}: rc={result.returncode} {result.stdout.strip()}")
    if result.returncode != 0:
        return None
    match = _VERSION_RE.search(result.stdout)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def check_version():
    """Raise RuntimeError unless the OpenShell CLI and gateway meet MIN_OPENSHELL_VERSION."""
    required = ".".join(str(part) for part in MIN_OPENSHELL_VERSION)
    for binary in _VERSION_BINARIES:
        version = _binary_version(binary)
        if version is None:
            raise RuntimeError(
                f"OpenShell >= {required} required; could not read the {binary} version"
            )
        if version < MIN_OPENSHELL_VERSION:
            found = ".".join(str(part) for part in version)
            raise RuntimeError(f"OpenShell >= {required} required; {binary} is {found}")


def start():
    """Start the OpenShell gateway with the podman driver.

    Checks the OpenShell version first, then starts the podman API socket,
    generates TLS certificates for sandbox JWT auth, writes a gateway
    config, launches openshell-gateway in the background, registers it with
    the CLI, and blocks until the health endpoint responds.

    If any step fails after processes have been spawned, cleanup is
    performed automatically to avoid orphaned processes.
    """
    check_version()

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

        for label, env_name, _key in _DRIVER_IMAGES:
            image = os.environ.get(env_name)
            if image:
                print(f"  {label}: {image}", flush=True)

        state_dir = os.path.expanduser("~/.local/state/openshell")
        os.makedirs(state_dir, exist_ok=True)
        log_file = tempfile.NamedTemporaryFile(
            mode="w",
            dir=state_dir,
            prefix="gateway-",
            suffix=".log",
            delete=False,
        )
        database_url = _create_database_url(state_dir)
        subprocess.Popen(
            [
                "openshell-gateway",
                "--db-url",
                database_url,
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


def _create_database_url(state_dir):
    """Create a temporary file-backed SQLite database for the gateway."""
    global _GATEWAY_DB_PATH

    database_file = tempfile.NamedTemporaryFile(
        dir=state_dir,
        prefix="gateway-",
        suffix=".db",
        delete=False,
    )
    database_file.close()
    _GATEWAY_DB_PATH = database_file.name
    return f"sqlite:{_GATEWAY_DB_PATH}?mode=rwc"


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
    _remove_database()
    _kill_podman_service()


def _remove_database():
    """Remove the temporary gateway database and any SQLite sidecar files."""
    global _GATEWAY_DB_PATH

    if _GATEWAY_DB_PATH is None:
        return
    for suffix in ("", "-journal", "-shm", "-wal"):
        try:
            os.remove(f"{_GATEWAY_DB_PATH}{suffix}")
        except FileNotFoundError:
            pass
    _GATEWAY_DB_PATH = None


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


def _config_path():
    return os.path.expanduser("~/.config/openshell/gateway.toml")


def _render_config():
    """Render the full gateway TOML config from the current environment."""
    return _GATEWAY_TOML.format(port=GATEWAY_PORT) + _render_podman_driver_section()


def config_is_current():
    """Return True when the on-disk gateway.toml matches the current environment.

    The gateway only reads gateway.toml at startup, so a running gateway
    keeps using the driver images it was started with. Callers use this to
    decide whether an already-running gateway can be reused or must be
    restarted after the supervisor or sandbox runtime image changed.
    """
    try:
        with open(_config_path()) as f:
            return f.read() == _render_config()
    except OSError:
        # Missing or unreadable: treat it as stale so it is rewritten.
        return False


def _write_config():
    """Write the gateway TOML config, updating it if the content changed."""
    if config_is_current():
        return
    # Render before opening the file, so a refused value leaves no
    # truncated gateway.toml behind.
    content = _render_config()
    config_path = _config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        f.write(content)


_TOML_UNSAFE_CHARS = frozenset('"\\\n\r')


def _render_podman_driver_section():
    """Render the ``[openshell.drivers.podman]`` block from the image env vars.

    Returns an empty string when neither image env var is set so the
    driver falls back to its compiled-in defaults.
    """
    lines = []
    for _label, env_name, key in _DRIVER_IMAGES:
        image = os.environ.get(env_name)
        if image:
            # The value goes into a TOML basic string unescaped. An image
            # reference never contains these characters, so refuse them
            # rather than write a gateway.toml the gateway cannot load.
            if _TOML_UNSAFE_CHARS.intersection(image):
                raise RuntimeError(
                    f"{env_name} contains characters not allowed in an image reference"
                )
            lines.append(f'{key} = "{image}"')
    if not lines:
        return ""
    return "\n[openshell.drivers.podman]\n" + "\n".join(lines) + "\n"


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
