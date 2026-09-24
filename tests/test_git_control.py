"""Tests for restoring host git control files after an agent run.

These use real git repositories: the point is that a host git command run
after :func:`restore_git_control` does not execute anything the agent
configured, which only git itself can confirm.
"""

import os
import subprocess
import threading
from unittest import mock

import pytest

from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.backends.podman import PodmanBackend
from agentic_ci.git import (
    GitControlTamperError,
    harden_git_config,
    restore_git_control,
    snapshot_git_control,
)
from agentic_ci.harness import ClaudeCodeHarness


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    """Keep the developer's global and system git config out of the tests."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("FAKE_SECRET", "s3cret")


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.fixture()
def host_repo(tmp_path):
    """A hardened clone with a bare origin, as a CI runner prepares it."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.name", "Bot")
    _git(repo, "config", "user.email", "bot@example.com")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "app.py").write_text("print('hi')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "push", "-q", "origin", "main")
    harden_git_config(repo)
    return repo


def _agent_run(repo, marker):
    """What an agent can do to .git from inside the sandbox.

    It commits a legitimate fix first, then plants config, hooks and
    attributes that make later host git commands run *marker*-writing
    scripts with the host's environment.
    """
    (repo / "app.py").write_text("print('fixed')\n")
    _git(repo, "commit", "-q", "-am", "agent fix")

    payload = repo / ".githooks"
    for name in ("pre-commit", "post-commit", "pre-push"):
        _script(payload / name, f'echo "{name} $FAKE_SECRET" >> {marker}')
    _script(payload / "fsmonitor", f'echo "fsmonitor $FAKE_SECRET" >> {marker}')
    _script(payload / "clean", f'echo "filter $FAKE_SECRET" >> {marker}; cat')
    _script(repo / ".git" / "hooks" / "pre-push", f'echo "git-hooks $FAKE_SECRET" >> {marker}')

    _git(repo, "config", "core.hooksPath", str(payload))
    _git(repo, "config", "core.fsmonitor", str(payload / "fsmonitor"))
    _git(repo, "config", "filter.evil.clean", str(payload / "clean"))
    (repo / ".git" / "info" / "attributes").write_text("* filter=evil\n")


def _host_git(repo):
    """The host-side git a runner performs after the agent (strip, amend, push)."""
    (repo / "verdict.json").write_text("{}\n")
    _git(repo, "add", "verdict.json")
    _git(repo, "commit", "-q", "--amend", "--no-edit")
    _git(repo, "rm", "-q", "--cached", "verdict.json")
    _git(repo, "commit", "-q", "--amend", "--no-edit", "--allow-empty")
    _git(repo, "push", "-q", "--force-with-lease", "origin", "main")


def test_agent_payload_runs_on_host_without_restore(host_repo, tmp_path):
    """Control: the planted config really executes, so the next test means something."""
    marker = tmp_path / "marker"
    _agent_run(host_repo, marker)

    _host_git(host_repo)

    ran = marker.read_text()
    assert "pre-commit s3cret" in ran
    assert "pre-push s3cret" in ran
    assert "fsmonitor s3cret" in ran
    assert "filter s3cret" in ran


def test_restore_stops_agent_git_config_running_on_host(host_repo, tmp_path):
    marker = tmp_path / "marker"
    config_before = (host_repo / ".git" / "config").read_bytes()
    snapshot = snapshot_git_control(host_repo)

    _agent_run(host_repo, marker)
    changed = restore_git_control(snapshot)
    _host_git(host_repo)

    assert not marker.exists()
    assert changed == ["config", "hooks", "info"]
    assert (host_repo / ".git" / "config").read_bytes() == config_before
    assert not (host_repo / ".git" / "info" / "attributes").exists()
    assert not (host_repo / ".git" / "hooks" / "pre-push").exists()
    # The agent's commit survives and reaches the remote.
    assert _git(host_repo, "log", "-1", "--format=%s") == "agent fix"
    assert _git(host_repo, "rev-parse", "HEAD") == _git(host_repo, "rev-parse", "origin/main")
    assert _git(host_repo, "config", "core.hooksPath") == "/dev/null"


def test_restore_removes_commondir_redirect(host_repo, tmp_path):
    """commondir makes git read config from another directory the agent controls."""
    marker = tmp_path / "marker"
    snapshot = snapshot_git_control(host_repo)

    evil = host_repo / ".evil-git"
    _git(host_repo, "clone", "-q", "--bare", str(host_repo), str(evil))
    _script(evil / "hooks-dir" / "pre-commit", f"echo commondir >> {marker}")
    _git(evil, "config", "core.hooksPath", str(evil / "hooks-dir"))
    (host_repo / ".git" / "commondir").write_text("../.evil-git\n")
    assert _git(host_repo, "config", "core.hooksPath") == str(evil / "hooks-dir")

    assert restore_git_control(snapshot) == ["commondir"]
    _git(host_repo, "commit", "-q", "--allow-empty", "-m", "host")

    assert not marker.exists()
    assert _git(host_repo, "config", "core.hooksPath") == "/dev/null"


def test_restore_does_not_write_through_planted_symlinks(host_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("host file\n")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    snapshot = snapshot_git_control(host_repo)

    (host_repo / ".git" / "config").unlink()
    (host_repo / ".git" / "config").symlink_to(outside)
    _remove_tree(host_repo / ".git" / "info")
    (host_repo / ".git" / "info").symlink_to(outside_dir, target_is_directory=True)

    restore_git_control(snapshot)

    assert outside.read_text() == "host file\n"
    assert list(outside_dir.iterdir()) == []
    assert not (host_repo / ".git" / "config").is_symlink()
    assert not (host_repo / ".git" / "info").is_symlink()
    assert _git(host_repo, "config", "user.name") == "Bot"


def _remove_tree(path):
    for child in sorted(path.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    path.rmdir()


def test_restore_preserves_file_modes(host_repo):
    hook = host_repo / ".git" / "hooks" / "pre-commit.sample"
    mode = hook.stat().st_mode
    snapshot = snapshot_git_control(host_repo)
    hook.chmod(0o600)

    assert restore_git_control(snapshot) == ["hooks"]
    assert hook.stat().st_mode == mode


def test_restore_is_a_no_op_when_nothing_changed(host_repo):
    snapshot = snapshot_git_control(host_repo)
    assert restore_git_control(snapshot) == []


def test_replaced_git_dir_raises_and_is_removed(host_repo, tmp_path):
    fake = tmp_path / "fake-git"
    fake.mkdir()
    snapshot = snapshot_git_control(host_repo)

    os.rename(host_repo / ".git", tmp_path / "real-git")
    (host_repo / ".git").symlink_to(fake, target_is_directory=True)

    with pytest.raises(GitControlTamperError, match="replaced by a symlink"):
        restore_git_control(snapshot)
    assert not os.path.lexists(host_repo / ".git")


def test_gitfile_pointer_is_restored(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /outside/main/.git/worktrees/wt\n")
    snapshot = snapshot_git_control(repo)

    (repo / ".git").write_text("gitdir: .evil\n")

    assert restore_git_control(snapshot) == [".git"]
    assert (repo / ".git").read_text() == "gitdir: /outside/main/.git/worktrees/wt\n"


def test_non_repo_workdir_is_left_alone(tmp_path):
    snapshot = snapshot_git_control(tmp_path)
    assert snapshot.dot_git is None
    assert restore_git_control(snapshot) == []
    assert not os.path.lexists(tmp_path / ".git")


def test_agent_created_git_dir_is_removed(host_repo, tmp_path):
    """A workdir inside a host repo: the agent's own .git would shadow the host's."""
    marker = tmp_path / "marker"
    workdir = host_repo / "sub"
    workdir.mkdir()
    snapshot = snapshot_git_control(workdir)
    assert snapshot.dot_git is None

    _git(tmp_path, "init", "-q", str(workdir))
    _script(tmp_path / "fsmonitor", f'echo "fsmonitor $FAKE_SECRET" >> {marker}')
    _git(workdir, "config", "core.fsmonitor", str(tmp_path / "fsmonitor"))

    assert restore_git_control(snapshot) == [".git"]
    assert not os.path.lexists(workdir / ".git")
    _git(workdir, "status")
    assert not marker.exists()


def _plant_diff_external(repo, marker):
    """Config that runs on a host ``git diff``, which callers run even after a failure."""
    _script(repo / ".githooks" / "differ", f'echo "diff $FAKE_SECRET" >> {marker}')
    _git(repo, "config", "diff.external", str(repo / ".githooks" / "differ"))


def _untrusted_leftovers(repo):
    return [p.name for p in (repo / ".git").iterdir() if "agentic-ci-untrusted" in p.name]


def test_restore_survives_deeply_nested_agent_tree(host_repo, tmp_path):
    """A tree deeper than the recursion limit must not stop the restore early."""
    marker = tmp_path / "marker"
    snapshot = snapshot_git_control(host_repo)
    _agent_run(host_repo, marker)
    _plant_diff_external(host_repo, marker)
    deep = host_repo / ".git" / "hooks"
    for _ in range(1200):
        deep = deep / "d"
        deep.mkdir()

    assert restore_git_control(snapshot) == ["config", "hooks", "info"]
    _git(host_repo, "diff", "HEAD~1...HEAD")

    assert not marker.exists()
    assert not (host_repo / ".git" / "hooks" / "d").exists()
    assert _untrusted_leftovers(host_repo) == []


def test_restore_does_not_read_huge_agent_files(host_repo, tmp_path):
    marker = tmp_path / "marker"
    snapshot = snapshot_git_control(host_repo)
    _plant_diff_external(host_repo, marker)
    with open(host_repo / ".git" / "info" / "huge", "wb") as fh:
        fh.truncate(1 << 34)  # Sparse, so it takes no disk space.

    assert restore_git_control(snapshot) == ["config", "info"]
    _git(host_repo, "diff", "HEAD")

    assert not marker.exists()
    assert not (host_repo / ".git" / "info" / "huge").exists()


def test_agent_permissions_do_not_block_restore(host_repo, tmp_path):
    marker = tmp_path / "marker"
    dot_git = host_repo / ".git"
    git_mode = dot_git.stat().st_mode
    snapshot = snapshot_git_control(host_repo)
    _plant_diff_external(host_repo, marker)
    locked = dot_git / "hooks" / "locked"
    locked.mkdir()
    (locked / "payload").write_text("x\n")
    locked.chmod(0)
    dot_git.chmod(0o500)

    restore_git_control(snapshot)
    _git(host_repo, "diff", "HEAD")

    assert not marker.exists()
    assert dot_git.stat().st_mode == git_mode
    assert not locked.exists()
    assert _untrusted_leftovers(host_repo) == []


def test_restore_fails_closed_when_host_copy_cannot_be_written(host_repo, tmp_path, monkeypatch):
    """A half-restored .git must not reach host git: it is moved aside instead."""
    marker = tmp_path / "marker"
    snapshot = snapshot_git_control(host_repo)
    _plant_diff_external(host_repo, marker)

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr("agentic_ci.git._write_entry", fail)

    with pytest.raises(GitControlTamperError, match="moved .* aside"):
        restore_git_control(snapshot)

    assert not os.path.lexists(host_repo / ".git")
    assert [p.name for p in host_repo.iterdir() if "agentic-ci-untrusted" in p.name] == []
    assert not marker.exists()


def test_gitfile_replaced_by_directory_is_restored(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /outside/main/.git/worktrees/wt\n")
    snapshot = snapshot_git_control(repo)

    (repo / ".git").unlink()
    _git(tmp_path, "init", "-q", str(repo))

    assert restore_git_control(snapshot) == [".git"]
    assert (repo / ".git").read_text() == "gitdir: /outside/main/.git/worktrees/wt\n"
    assert [p.name for p in repo.iterdir()] == [".git"]


def test_restore_puts_back_ownership_as_root(host_repo, monkeypatch):
    """The Podman root path chowns the workdir to the container user; keep that."""
    snapshot = snapshot_git_control(host_repo)
    owners = {
        str(host_repo / ".git" / rel): (entry.uid, entry.gid)
        for rel, entry in snapshot.entries.items()
    }
    assert owners
    chowned = {}
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        os, "lchown", lambda path, uid, gid: chowned.update({str(path): (uid, gid)})
    )

    restore_git_control(snapshot)

    assert chowned == owners


# -- Backends -----------------------------------------------------------------


def test_openshell_run_restores_git_after_download(host_repo, tmp_path, monkeypatch):
    """The workdir download carries the agent's .git; host git must not honor it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    marker = tmp_path / "marker"
    backend = OpenShellBackend(workdir=str(host_repo), harness=ClaudeCodeHarness())

    with (
        mock.patch.object(backend, "_write_env_script"),
        mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd_streaming"),
        mock.patch(
            "agentic_ci.backends.openshell.sandbox.download",
            side_effect=lambda *_: _agent_run(host_repo, marker),
        ),
        mock.patch.object(backend, "_process_stream", return_value=(0, True)),
        mock.patch.object(backend, "_wait_for_otel_flush"),
    ):
        assert backend.run("prompt", "claude-opus-4-6") == 0

    _host_git(host_repo)
    assert not marker.exists()
    assert _git(host_repo, "log", "-1", "--format=%s") == "agent fix"
    assert backend._host_git is None


def test_openshell_run_restores_git_when_download_fails(host_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    marker = tmp_path / "marker"
    backend = OpenShellBackend(workdir=str(host_repo), harness=ClaudeCodeHarness())

    def partial_download(*_):
        _agent_run(host_repo, marker)
        raise subprocess.CalledProcessError(1, "openshell sandbox download")

    with (
        mock.patch.object(backend, "_write_env_script"),
        mock.patch("agentic_ci.backends.openshell.sandbox.exec_cmd_streaming"),
        mock.patch("agentic_ci.backends.openshell.sandbox.download", side_effect=partial_download),
        mock.patch.object(backend, "_process_stream", return_value=(0, True)),
        mock.patch.object(backend, "_wait_for_otel_flush"),
        pytest.raises(subprocess.CalledProcessError) as exc_info,
    ):
        backend.run("prompt", "claude-opus-4-6")

    # The download failure propagates, not a git error from the agent's steps.
    assert exc_info.value.cmd == "openshell sandbox download"
    _host_git(host_repo)
    assert not marker.exists()


_real_run = subprocess.run
_real_popen = subprocess.Popen


def _fake_podman(handler=None):
    """Patch ``subprocess.run`` so only podman and chown commands are faked.

    ``subprocess`` is one module, so patching it through the backend also
    patches the git calls these tests make; those still run for real.
    ``chown`` is faked because setup() runs ``chown -R 1000:1000`` when the
    tests run as root (CI containers), after which git refuses the repo as
    having dubious ownership.
    """
    calls = []

    def run(cmd, **kwargs):
        if cmd[0] == "chown":
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] != "podman":
            return _real_run(cmd, **kwargs)
        calls.append(cmd[:2])
        if handler is not None:
            return handler(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    def popen(cmd, **kwargs):
        return mock.MagicMock() if cmd[0] == "podman" else _real_popen(cmd, **kwargs)

    patches = (
        mock.patch("agentic_ci.backends.podman.subprocess.run", side_effect=run),
        mock.patch("agentic_ci.backends.podman.subprocess.Popen", side_effect=popen),
    )
    return patches, calls


def _podman_backend(repo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    backend = PodmanBackend(
        workdir=str(repo), image="localhost/test:latest", harness=ClaudeCodeHarness()
    )
    (run_patch, popen_patch), _ = _fake_podman()
    with (
        mock.patch.object(backend, "_resolve_sandbox_config"),
        mock.patch.object(backend, "is_running", return_value=False),
        run_patch,
        popen_patch,
    ):
        backend.setup()
    assert backend._host_git is not None
    return backend


def _run_podman(backend, stream, handler=None):
    (run_patch, popen_patch), calls = _fake_podman(handler)
    with (
        mock.patch.object(backend, "is_running", return_value=True),
        mock.patch.object(backend, "_process_stream", side_effect=stream),
        mock.patch.object(backend, "_wait_for_otel_flush"),
        run_patch,
        popen_patch,
    ):
        assert backend.run("prompt", "model") == 0
    return calls


def test_podman_setup_does_not_snapshot_a_running_container(host_repo, monkeypatch):
    """An agent may already have written .git; that copy must not become the baseline."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    backend = PodmanBackend(
        workdir=str(host_repo), image="localhost/test:latest", harness=ClaudeCodeHarness()
    )
    with (
        mock.patch.object(backend, "_resolve_sandbox_config"),
        mock.patch.object(backend, "is_running", return_value=True),
    ):
        backend.setup()
    assert backend._host_git is None


def test_podman_run_restores_git_written_through_bind_mount(host_repo, tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    backend = _podman_backend(host_repo, monkeypatch)

    def agent_stream(*_):
        _agent_run(host_repo, marker)
        return 0, True

    _run_podman(backend, agent_stream)

    _host_git(host_repo)
    assert not marker.exists()
    assert _git(host_repo, "log", "-1", "--format=%s") == "agent fix"


def _plant_config(repo, marker):
    """A leftover agent process re-planting an fsmonitor through the bind mount."""
    _script(repo / ".githooks" / "late-fsmonitor", f'echo "late $FAKE_SECRET" >> {marker}')
    _git(repo, "config", "core.fsmonitor", str(repo / ".githooks" / "late-fsmonitor"))


def test_podman_run_kills_agent_leftovers_before_restoring(host_repo, tmp_path, monkeypatch):
    """No agent process may outlive run(): it could rewrite .git while host git runs."""
    marker = tmp_path / "marker"
    backend = _podman_backend(host_repo, monkeypatch)
    killed = threading.Event()

    def leftover_writer():
        # Stands in for a process the agent daemonized in the container; it
        # keeps writing through the bind mount until the container stops.
        while not killed.wait(0.005):
            _plant_config(host_repo, marker)

    writer = threading.Thread(target=leftover_writer, daemon=True)

    def agent_stream(*_):
        _agent_run(host_repo, marker)
        _plant_config(host_repo, marker)
        writer.start()
        return 0, True

    def podman(cmd):
        if cmd[:2] == ["podman", "stop"]:
            # What the leftover writer planted is still there when the
            # container stops; the restore after this must remove it.
            assert "late-fsmonitor" in _git(host_repo, "config", "core.fsmonitor")
            killed.set()
            writer.join()
        return subprocess.CompletedProcess(cmd, 0)

    try:
        calls = _run_podman(backend, agent_stream, podman)
        assert not writer.is_alive()
        _host_git(host_repo)
        assert not marker.exists()
        assert calls == [["podman", "stop"]]
    finally:
        killed.set()
        if writer.is_alive():
            writer.join()


def test_podman_run_removes_container_when_stop_fails(host_repo, tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    backend = _podman_backend(host_repo, monkeypatch)

    def agent_stream(*_):
        _agent_run(host_repo, marker)
        return 0, True

    def podman(cmd):
        if cmd[:2] == ["podman", "stop"]:
            return subprocess.CompletedProcess(cmd, 125, stderr=b"boom")
        # The container is removed before the host copy is restored.
        assert _git(host_repo, "config", "core.hooksPath")
        return subprocess.CompletedProcess(cmd, 0)

    calls = _run_podman(backend, agent_stream, podman)

    assert calls == [["podman", "stop"], ["podman", "rm"]]
    assert backend._parked is False
    _host_git(host_repo)
    assert not marker.exists()


def test_podman_run_discards_git_when_container_cannot_be_removed(host_repo, tmp_path, monkeypatch):
    """A container that survives may still hold agent processes; .git is not trusted."""
    marker = tmp_path / "marker"
    backend = _podman_backend(host_repo, monkeypatch)

    def agent_stream(*_):
        _agent_run(host_repo, marker)
        return 0, True

    podman_calls = []

    def podman(cmd):
        podman_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 125, stderr=b"boom")

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _run_podman(backend, agent_stream, podman)

    # The error is the failed removal, not a git error from the agent's steps.
    container = backend._container_name
    assert exc_info.value.cmd == ["podman", "rm", "-f", container]
    assert podman_calls == [
        ["podman", "stop", "--time", "0", container],
        ["podman", "rm", "-f", container],
    ]
    assert not os.path.lexists(host_repo / ".git")
    assert backend._host_git is None
    assert not marker.exists()


def test_podman_next_run_restarts_the_stopped_container(host_repo, monkeypatch):
    backend = _podman_backend(host_repo, monkeypatch)
    _run_podman(backend, lambda *_: (0, True))
    assert backend._parked is True

    with mock.patch.object(backend, "setup") as setup:
        calls = _run_podman(backend, lambda *_: (0, True))

    setup.assert_not_called()
    assert calls == [["podman", "start"], ["podman", "stop"]]
    assert backend._parked is True


def test_podman_next_run_keeps_host_git_config_edited_between_runs(
    host_repo, tmp_path, monkeypatch
):
    """Each run restores the host copy taken when it started, not the one from setup()."""
    marker = tmp_path / "marker"
    backend = _podman_backend(host_repo, monkeypatch)
    _run_podman(backend, lambda *_: (0, True))

    # Host-side git between runs, for example a runner updating the remote.
    _git(host_repo, "config", "agentic-ci.host-edit", "kept")

    def agent_stream(*_):
        _agent_run(host_repo, marker)
        return 0, True

    def podman(cmd):
        if cmd[:2] == ["podman", "start"]:
            # Recorded before the container, and so the agent, can run again.
            assert b"host-edit" in backend._host_git.entries["config"].data
        return subprocess.CompletedProcess(cmd, 0)

    calls = _run_podman(backend, agent_stream, podman)

    assert calls == [["podman", "start"], ["podman", "stop"]]
    probe = subprocess.run(
        ["git", "config", "--get", "agentic-ci.host-edit"],
        cwd=str(host_repo),
        capture_output=True,
        text=True,
    )
    assert (probe.returncode, probe.stdout.strip()) == (0, "kept")
    _host_git(host_repo)
    assert not marker.exists()
    assert backend._host_git is None


def test_podman_stop_restores_git_written_after_setup(host_repo, tmp_path, monkeypatch):
    """stop() restores even when run() never stopped the container itself."""
    marker = tmp_path / "marker"
    backend = _podman_backend(host_repo, monkeypatch)
    _agent_run(host_repo, marker)

    (run_patch, popen_patch), calls = _fake_podman()
    with run_patch, popen_patch:
        backend.stop()

    assert calls == [["podman", "rm"]]
    _host_git(host_repo)
    assert not marker.exists()
    assert backend._host_git is None
