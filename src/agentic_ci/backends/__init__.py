"""Backend registry and factory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

    Returns:
        A Backend instance.
    """
    if name == "podman":
        return PodmanBackend(
            workdir=kwargs.get("workdir", "."),
            image=kwargs.get("image"),
            timeout=kwargs.get("timeout", 1200),
            extra_env=kwargs.get("extra_env"),
            harness=harness,
        )
    elif name == "openshell":
        return OpenShellBackend(
            workdir=kwargs.get("workdir", "."),
            image=kwargs.get("image"),
            policy=kwargs.get("policy"),
            extra_env=kwargs.get("extra_env"),
            approval_mode=kwargs.get("approval_mode"),
            harness=harness,
        )
    else:
        raise ValueError(f"Unknown backend: {name!r}. Choose 'podman' or 'openshell'.")
