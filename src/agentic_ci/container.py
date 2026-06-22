"""Auto-configure podman storage for container-in-container environments.

When agentic-ci runs inside a CI container (GitHub Actions, GitLab CI,
Prow), the inner podman needs a storage driver compatible with nested
execution. Default overlay-on-overlay fails without fuse-overlayfs.

This module detects the nested scenario and writes a working
``/etc/containers/storage.conf`` if one is not already present.
"""

from __future__ import annotations

import os
import shutil

from agentic_ci import log

_STORAGE_CONF = "/etc/containers/storage.conf"
_SUBUID = "/etc/subuid"
_SUBGID = "/etc/subgid"

_OVERLAY_CONF = (
    "[storage]\n"
    'driver = "overlay"\n'
    "[storage.options.overlay]\n"
    'mount_program = "/usr/bin/fuse-overlayfs"\n'
)

_VFS_CONF = '[storage]\ndriver = "vfs"\n'


def is_in_container() -> bool:
    """Return True if the current process is running inside a container."""
    return os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")


def _is_storage_configured() -> bool:
    """Return True if storage.conf already has a nested-safe driver."""
    try:
        with open(_STORAGE_CONF) as f:
            content = f.read()
    except (FileNotFoundError, PermissionError):
        return False

    if 'driver = "vfs"' in content:
        return True
    if 'driver = "overlay"' in content and "mount_program" in content:
        return True
    return False


def configure_podman_storage() -> None:
    """Detect container-in-container and configure podman storage if needed.

    No-op when not running inside a container or when storage is already
    configured with a nested-safe driver (vfs, or overlay with
    fuse-overlayfs).
    """
    if not is_in_container():
        return

    if _is_storage_configured():
        return

    use_overlay = shutil.which("fuse-overlayfs") is not None

    log.section("Configuring podman storage for nested container")

    try:
        os.makedirs(os.path.dirname(_STORAGE_CONF), exist_ok=True)

        if use_overlay:
            with open(_STORAGE_CONF, "w") as f:
                f.write(_OVERLAY_CONF)
            log.detail("Driver", "overlay (fuse-overlayfs)")
        else:
            with open(_STORAGE_CONF, "w") as f:
                f.write(_VFS_CONF)
            for path in (_SUBUID, _SUBGID):
                with open(path, "w") as f:
                    f.truncate(0)
            log.detail("Driver", "vfs")
    except PermissionError:
        log.info("WARNING: cannot write storage config (permission denied)")
