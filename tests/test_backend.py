"""Tests for backend factory."""

import pytest

from agentic_ci.backends import create_backend
from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.backends.podman import PodmanBackend


def test_create_podman_backend():
    backend = create_backend("podman", workdir="/tmp")
    assert isinstance(backend, PodmanBackend)
    assert backend.workdir == "/tmp"


def test_create_openshell_backend():
    backend = create_backend("openshell", workdir="/tmp")
    assert isinstance(backend, OpenShellBackend)
    assert backend.workdir == "/tmp"


def test_create_podman_with_image():
    backend = create_backend("podman", image="my-image:latest")
    assert backend.image == "my-image:latest"


def test_create_openshell_with_policy():
    backend = create_backend("openshell", policy="/path/to/policy.yml")
    assert backend.policy_path == "/path/to/policy.yml"


def test_create_podman_with_timeout():
    backend = create_backend("podman", timeout=600)
    assert backend.timeout == 600


def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown backend"):
        create_backend("docker")


def test_backends_have_stop_method():
    podman = create_backend("podman", workdir="/tmp")
    openshell = create_backend("openshell", workdir="/tmp")
    assert callable(getattr(podman, "stop", None))
    assert callable(getattr(openshell, "stop", None))
