"""Backend registry and factory."""

from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.backends.podman import PodmanBackend


def create_backend(name, **kwargs):
    """Create a backend instance by name.

    Args:
        name: Backend name ("podman" or "openshell").
        **kwargs: Backend-specific arguments (workdir, image, policy, timeout, etc.).

    Returns:
        A Backend instance.
    """
    if name == "podman":
        return PodmanBackend(
            workdir=kwargs.get("workdir", "."),
            image=kwargs.get("image"),
            timeout=kwargs.get("timeout", 1200),
        )
    elif name == "openshell":
        return OpenShellBackend(
            workdir=kwargs.get("workdir", "."),
            image=kwargs.get("image"),
            policy=kwargs.get("policy"),
        )
    else:
        raise ValueError(f"Unknown backend: {name!r}. Choose 'podman' or 'openshell'.")
