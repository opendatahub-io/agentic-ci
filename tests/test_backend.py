"""Tests for backend factory."""

import pytest

from agentic_ci.backends import create_backend
from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.backends.podman import PodmanBackend
from agentic_ci.harness import create_harness


@pytest.fixture()
def harness():
    return create_harness("claude-code")


def test_create_podman_backend(harness):
    backend = create_backend("podman", harness=harness, workdir="/tmp")
    assert isinstance(backend, PodmanBackend)
    assert backend.workdir == "/tmp"
    assert backend.harness is harness


def test_create_openshell_backend(harness):
    backend = create_backend("openshell", harness=harness, workdir="/tmp")
    assert isinstance(backend, OpenShellBackend)
    assert backend.workdir == "/tmp"
    assert backend.harness is harness


def test_create_podman_with_image(harness):
    backend = create_backend("podman", harness=harness, image="my-image:latest")
    assert backend.image == "my-image:latest"


def test_create_openshell_with_policy(harness):
    backend = create_backend("openshell", harness=harness, policy="/path/to/policy.yml")
    assert backend.policy_path == "/path/to/policy.yml"


def test_create_podman_with_timeout(harness):
    backend = create_backend("podman", harness=harness, timeout=600)
    assert backend.timeout == 600


def test_unknown_backend_raises(harness):
    with pytest.raises(ValueError, match="Unknown backend"):
        create_backend("docker", harness=harness)


def test_backends_have_stop_method(harness):
    podman = create_backend("podman", harness=harness, workdir="/tmp")
    openshell = create_backend("openshell", harness=harness, workdir="/tmp")
    assert callable(getattr(podman, "stop", None))
    assert callable(getattr(openshell, "stop", None))
