"""Tests for OpenShell gateway lifecycle management."""

from pathlib import Path
from unittest import mock

from agentic_ci.backends.openshell import gateway


def test_start_uses_file_backed_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gateway, "_GATEWAY_DB_PATH", None)

    with (
        mock.patch.object(gateway, "_wait_for_socket"),
        mock.patch.object(gateway, "_write_config"),
        mock.patch.object(gateway, "_generate_certs"),
        mock.patch.object(gateway, "_register"),
        mock.patch.object(gateway, "is_running", return_value=True),
        mock.patch.object(gateway.subprocess, "Popen") as popen,
    ):
        gateway.start()

    gateway_args = popen.call_args_list[1].args[0]
    database_url = gateway_args[gateway_args.index("--db-url") + 1]

    assert database_url.startswith("sqlite:")
    assert database_url.endswith(".db?mode=rwc")
    database_path = Path(database_url.removeprefix("sqlite:").removesuffix("?mode=rwc"))
    assert database_path.is_file()
    database_path.unlink()


def test_stop_removes_gateway_database(monkeypatch, tmp_path):
    database_path = tmp_path / "gateway.db"
    database_files = [
        database_path,
        Path(f"{database_path}-journal"),
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-wal"),
    ]
    for path in database_files:
        path.touch()
    monkeypatch.setattr(gateway, "_GATEWAY_DB_PATH", str(database_path))

    with (
        mock.patch.object(gateway.subprocess, "run", return_value=mock.Mock(returncode=0)),
        mock.patch.object(gateway, "_kill_gateway"),
        mock.patch.object(gateway, "_kill_podman_service"),
    ):
        gateway.stop()

    assert not any(path.exists() for path in database_files)
    assert gateway._GATEWAY_DB_PATH is None


def _read_gateway_config(home):
    return (Path(home) / ".config" / "openshell" / "gateway.toml").read_text()


def test_write_config_without_driver_images(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENSHELL_SUPERVISOR_IMAGE", raising=False)
    monkeypatch.delenv("OPENSHELL_SANDBOX_RUNTIME_IMAGE", raising=False)

    gateway._write_config()

    rendered = _read_gateway_config(tmp_path)
    assert "version = 2" in rendered
    assert 'bind_address = "0.0.0.0:17670"' in rendered
    assert 'compute_driver = "podman"' in rendered
    assert "compute_drivers" not in rendered
    assert "[openshell.drivers.podman]" not in rendered


def test_write_config_renders_supervisor_and_sandbox_runtime_images(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENSHELL_SUPERVISOR_IMAGE", "quay.io/x/supervisor:v1")
    monkeypatch.setenv("OPENSHELL_SANDBOX_RUNTIME_IMAGE", "quay.io/x/sandbox:v1")

    gateway._write_config()

    rendered = _read_gateway_config(tmp_path)
    assert rendered.endswith(
        "\n[openshell.drivers.podman]\n"
        'supervisor_image = "quay.io/x/supervisor:v1"\n'
        'sandbox_runtime_image = "quay.io/x/sandbox:v1"\n'
    )
    assert rendered.count("[openshell.drivers.podman]") == 1


def test_write_config_renders_sandbox_runtime_image_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENSHELL_SUPERVISOR_IMAGE", raising=False)
    monkeypatch.setenv("OPENSHELL_SANDBOX_RUNTIME_IMAGE", "quay.io/x/sandbox:v1")

    gateway._write_config()

    rendered = _read_gateway_config(tmp_path)
    assert "[openshell.drivers.podman]" in rendered
    assert 'sandbox_runtime_image = "quay.io/x/sandbox:v1"' in rendered
    assert "supervisor_image" not in rendered


def test_write_config_rewrites_when_images_change(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENSHELL_SUPERVISOR_IMAGE", "quay.io/x/supervisor:v1")
    monkeypatch.setenv("OPENSHELL_SANDBOX_RUNTIME_IMAGE", "quay.io/x/sandbox:v1")
    gateway._write_config()

    monkeypatch.setenv("OPENSHELL_SANDBOX_RUNTIME_IMAGE", "quay.io/x/sandbox:v2")
    gateway._write_config()

    rendered = _read_gateway_config(tmp_path)
    assert 'sandbox_runtime_image = "quay.io/x/sandbox:v2"' in rendered
    assert "sandbox:v1" not in rendered


def test_config_is_current_false_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENSHELL_SUPERVISOR_IMAGE", raising=False)
    monkeypatch.delenv("OPENSHELL_SANDBOX_RUNTIME_IMAGE", raising=False)

    assert not gateway.config_is_current()


def test_config_is_current_tracks_driver_image_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENSHELL_SUPERVISOR_IMAGE", "quay.io/x/supervisor:v1")
    monkeypatch.setenv("OPENSHELL_SANDBOX_RUNTIME_IMAGE", "quay.io/x/sandbox:v1")
    gateway._write_config()
    assert gateway.config_is_current()

    monkeypatch.setenv("OPENSHELL_SANDBOX_RUNTIME_IMAGE", "quay.io/x/sandbox:v2")
    assert not gateway.config_is_current()
