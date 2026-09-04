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
