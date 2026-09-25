"""Backend registry and factory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentic_ci import log
from agentic_ci.backends.local import LocalBackend
from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.backends.podman import PodmanBackend

if TYPE_CHECKING:
    from agentic_ci.backend import Backend
    from agentic_ci.harness import Harness


def create_backend(name: str, *, harness: Harness, **kwargs: Any) -> Backend:
    """Create a backend instance by name.

    Args:
        name: Backend name ("podman" or "openshell").
        harness: Agent harness instance.
        **kwargs: Backend-specific arguments (workdir, image, policy, timeout, etc.).
            ``sandbox_profile`` (a :class:`~agentic_ci.sandbox_profile.SandboxProfile`)
            is passed only to the OpenShell backend; other backends log a
            warning and ignore it.

    Returns:
        A Backend instance.
    """
    sandbox_profile = kwargs.get("sandbox_profile")
    if sandbox_profile is not None and name in ("local", "podman"):
        log.info(
            f"WARNING: sandbox profiles only apply to the OpenShell backend; "
            f"ignoring the profile for the {name} backend"
        )
    if name == "local":
        return LocalBackend(
            workdir=kwargs.get("workdir", "."),
            extra_env=kwargs.get("extra_env"),
            harness=harness,
        )
    elif name == "podman":
        return PodmanBackend(
            workdir=kwargs.get("workdir", "."),
            image=kwargs.get("image"),
            timeout=kwargs.get("timeout", 1200),
            extra_env=kwargs.get("extra_env"),
            harness=harness,
        )
    elif name == "openshell":
        # The profile is passed only when set, so a run without one constructs
        # the backend exactly as before.
        profile_kwargs = {} if sandbox_profile is None else {"sandbox_profile": sandbox_profile}
        return OpenShellBackend(
            workdir=kwargs.get("workdir", "."),
            image=kwargs.get("image"),
            policy=kwargs.get("policy"),
            extra_env=kwargs.get("extra_env"),
            approval_mode=kwargs.get("approval_mode"),
            memory=kwargs.get("memory"),
            cpu=kwargs.get("cpu"),
            gpu=kwargs.get("gpu"),
            harness=harness,
            **profile_kwargs,
        )
    else:
        raise ValueError(f"Unknown backend: {name!r}. Choose 'local', 'podman', or 'openshell'.")
