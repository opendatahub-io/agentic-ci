"""Tests for container-in-container storage detection and configuration."""

import os

from agentic_ci.container import (
    _is_storage_configured,
    configure_podman_storage,
    is_in_container,
)


class TestIsInContainer:
    def test_not_in_container(self, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        assert is_in_container() is False

    def test_podman_container(self, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/run/.containerenv")
        assert is_in_container() is True

    def test_docker_container(self, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/.dockerenv")
        assert is_in_container() is True


class TestIsStorageConfigured:
    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agentic_ci.container._STORAGE_CONF",
            str(tmp_path / "nonexistent"),
        )
        assert _is_storage_configured() is False

    def test_vfs_configured(self, tmp_path, monkeypatch):
        conf = tmp_path / "storage.conf"
        conf.write_text('[storage]\ndriver = "vfs"\n')
        monkeypatch.setattr("agentic_ci.container._STORAGE_CONF", str(conf))
        assert _is_storage_configured() is True

    def test_overlay_with_fuse(self, tmp_path, monkeypatch):
        conf = tmp_path / "storage.conf"
        conf.write_text(
            '[storage]\ndriver = "overlay"\n'
            "[storage.options.overlay]\n"
            'mount_program = "/usr/bin/fuse-overlayfs"\n'
        )
        monkeypatch.setattr("agentic_ci.container._STORAGE_CONF", str(conf))
        assert _is_storage_configured() is True

    def test_overlay_without_fuse(self, tmp_path, monkeypatch):
        conf = tmp_path / "storage.conf"
        conf.write_text('[storage]\ndriver = "overlay"\n')
        monkeypatch.setattr("agentic_ci.container._STORAGE_CONF", str(conf))
        assert _is_storage_configured() is False


class TestConfigurePodmanStorage:
    def test_not_in_container_is_noop(self, tmp_path, monkeypatch):
        conf = tmp_path / "storage.conf"
        monkeypatch.setattr("agentic_ci.container._STORAGE_CONF", str(conf))
        monkeypatch.setattr(os.path, "exists", lambda p: False)

        configure_podman_storage()
        assert not conf.exists()

    def test_already_configured_is_noop(self, tmp_path, monkeypatch):
        conf = tmp_path / "storage.conf"
        original = '[storage]\ndriver = "vfs"\n'
        conf.write_text(original)
        monkeypatch.setattr("agentic_ci.container._STORAGE_CONF", str(conf))
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        configure_podman_storage()
        assert conf.read_text() == original

    def test_overlay_with_fuse_overlayfs(self, tmp_path, monkeypatch):
        conf = tmp_path / "containers" / "storage.conf"
        monkeypatch.setattr("agentic_ci.container._STORAGE_CONF", str(conf))
        monkeypatch.setattr(os.path, "exists", lambda p: True)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/fuse-overlayfs")

        configure_podman_storage()

        content = conf.read_text()
        assert 'driver = "overlay"' in content
        assert "fuse-overlayfs" in content

    def test_vfs_fallback(self, tmp_path, monkeypatch):
        conf = tmp_path / "containers" / "storage.conf"
        subuid = tmp_path / "subuid"
        subgid = tmp_path / "subgid"
        subuid.write_text("root:100000:65536\n")
        subgid.write_text("root:100000:65536\n")

        monkeypatch.setattr("agentic_ci.container._STORAGE_CONF", str(conf))
        monkeypatch.setattr("agentic_ci.container._SUBUID", str(subuid))
        monkeypatch.setattr("agentic_ci.container._SUBGID", str(subgid))
        monkeypatch.setattr(os.path, "exists", lambda p: True)
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        configure_podman_storage()

        assert 'driver = "vfs"' in conf.read_text()
        assert subuid.read_text() == ""
        assert subgid.read_text() == ""

    def test_permission_denied_does_not_crash(self, tmp_path, monkeypatch):
        conf = tmp_path / "readonly" / "storage.conf"
        monkeypatch.setattr("agentic_ci.container._STORAGE_CONF", str(conf))
        monkeypatch.setattr(os.path, "exists", lambda p: True)
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)

        try:
            configure_podman_storage()
        finally:
            readonly_dir.chmod(0o755)
