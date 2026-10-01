"""Tests for the deprecated host setup path (``allow_host_setup``)."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest
import yaml

from agentic_ci import backend as backend_module
from agentic_ci import cli
from agentic_ci.backend import CI_ENV_MARKERS, HostSetupRefusedError, running_in_ci
from agentic_ci.backends import create_backend
from agentic_ci.backends.local import LocalBackend
from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.backends.podman import PodmanBackend
from agentic_ci.harness import ClaudeCodeHarness

BACKEND_NAMES = ("local", "podman", "openshell")
PLANTED_SECRETS = {
    "OPENAI_API_KEY": "sk-planted-openai",
    "BOT_PAT": "planted-bot-pat",
    "GITHUB_TOKEN": "planted-github-token",
}


@pytest.fixture(autouse=True)
def _no_ci_markers(monkeypatch):
    """Tests that set the flag must also pass inside a CI job."""
    for name in CI_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AGENTIC_CI_SKIP_SETUP", raising=False)


@pytest.fixture()
def harness():
    return ClaudeCodeHarness()


@pytest.fixture()
def popen_spy(monkeypatch):
    """Record every Popen and run call while still running them."""
    calls = []
    real_popen = subprocess.Popen
    real_run = subprocess.run

    def spy_popen(*args, **kwargs):
        calls.append(("Popen", args, kwargs))
        return real_popen(*args, **kwargs)

    def spy_run(*args, **kwargs):
        calls.append(("run", args, kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(backend_module.subprocess, "Popen", spy_popen)
    monkeypatch.setattr(backend_module.subprocess, "run", spy_run)
    return calls


def _write_config(workdir: Path, *steps: str) -> None:
    (workdir / ".agentic-ci").mkdir(parents=True, exist_ok=True)
    config = {"setup": [{"name": f"step {i}", "run": run} for i, run in enumerate(steps)]}
    (workdir / ".agentic-ci" / "config.yml").write_text(yaml.safe_dump(config))


def _git_init(workdir: Path) -> None:
    subprocess.run(["git", "init", "-q", str(workdir)], check=True)


def _make(name, workdir, harness, **kwargs):
    return create_backend(name, harness=harness, workdir=str(workdir), **kwargs)


def _step_ran(calls, marker):
    return [c for c in calls if marker in str(c[1])]


def _process_gone(pid: int, timeout: float = 5.0) -> bool:
    """True once *pid* no longer exists or is a zombie."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state in ("Z", "X"):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class TestDefaultRunsNothing:
    """Without the flag no host step runs, on any backend."""

    @pytest.mark.parametrize("name", BACKEND_NAMES)
    def test_no_step_runs(self, tmp_path, harness, popen_spy, capsys, name):
        _write_config(tmp_path, "touch ran-marker", "echo secret-step-text")
        backend = _make(name, tmp_path, harness)

        backend._run_setup_steps()

        assert not (tmp_path / "ran-marker").exists()
        assert _step_ran(popen_spy, "ran-marker") == []
        out = capsys.readouterr().out
        assert "Host setup is disabled: 2 setup step(s)" in out
        # Only the count is logged, no text from the repo.
        assert "ran-marker" not in out
        assert "secret-step-text" not in out
        assert "step 0" not in out
        assert "deprecated" not in out

    @pytest.mark.parametrize("name", BACKEND_NAMES)
    def test_no_config_logs_nothing(self, tmp_path, harness, capsys, name):
        _make(name, tmp_path, harness)._run_setup_steps()
        assert capsys.readouterr().out == ""

    def test_local_setup_runs_no_step(self, tmp_path, harness, popen_spy, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _write_config(tmp_path, "touch ran-marker")
        _make("local", tmp_path, harness).setup()
        assert not (tmp_path / "ran-marker").exists()
        assert _step_ran(popen_spy, "ran-marker") == []

    def test_podman_setup_runs_no_step(self, tmp_path, harness, popen_spy, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _write_config(tmp_path, "touch ran-marker")
        backend = PodmanBackend(workdir=str(tmp_path), image="localhost/x:1", harness=harness)
        with (
            mock.patch.object(backend, "_resolve_sandbox_config"),
            mock.patch.object(backend, "is_running", return_value=True),
        ):
            backend.setup()
        assert not (tmp_path / "ran-marker").exists()
        assert _step_ran(popen_spy, "ran-marker") == []

    def test_default_backends_keep_their_constructor_arguments(self, harness):
        for name, cls_name in (
            ("local", "LocalBackend"),
            ("podman", "PodmanBackend"),
            ("openshell", "OpenShellBackend"),
        ):
            for kwargs in ({}, {"allow_host_setup": False}, {"allow_host_setup": "yes"}):
                with mock.patch(f"agentic_ci.backends.{cls_name}") as cls:
                    create_backend(name, harness=harness, **kwargs)
                assert "allow_host_setup" not in cls.call_args.kwargs, (name, kwargs)
            with mock.patch(f"agentic_ci.backends.{cls_name}") as cls:
                create_backend(name, harness=harness, allow_host_setup=True)
            assert cls.call_args.kwargs["allow_host_setup"] is True


class TestCiRefusal:
    @pytest.mark.parametrize("marker", CI_ENV_MARKERS)
    @pytest.mark.parametrize("value", ["true", "1", "TRUE", "yes"])
    @pytest.mark.parametrize("name", BACKEND_NAMES)
    def test_refused_in_ci(self, tmp_path, harness, monkeypatch, marker, value, name):
        monkeypatch.setenv(marker, value)
        with pytest.raises(HostSetupRefusedError) as exc:
            _make(name, tmp_path, harness, allow_host_setup=True)
        assert isinstance(exc.value, ValueError)
        assert str(exc.value) == str(HostSetupRefusedError())

    @pytest.mark.parametrize("marker", CI_ENV_MARKERS)
    @pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "False"])
    def test_not_refused_when_marker_is_off(self, tmp_path, harness, monkeypatch, marker, value):
        monkeypatch.setenv(marker, value)
        backend = _make("local", tmp_path, harness, allow_host_setup=True)
        assert backend.allow_host_setup is True

    @pytest.mark.parametrize("name", BACKEND_NAMES)
    def test_default_is_allowed_in_ci(self, tmp_path, harness, monkeypatch, name):
        monkeypatch.setenv("CI", "true")
        assert _make(name, tmp_path, harness).allow_host_setup is False

    @pytest.mark.parametrize("marker", CI_ENV_MARKERS)
    def test_refused_when_ci_appears_after_construction(
        self, tmp_path, harness, popen_spy, monkeypatch, marker
    ):
        _write_config(tmp_path, "touch ran-marker")
        backend = _make("local", tmp_path, harness, allow_host_setup=True)
        monkeypatch.setenv(marker, "true")
        with pytest.raises(HostSetupRefusedError):
            backend._run_setup_steps()
        assert not (tmp_path / "ran-marker").exists()
        assert _step_ran(popen_spy, "ran-marker") == []

    def test_refused_when_flag_is_set_after_construction_in_ci(
        self, tmp_path, harness, popen_spy, monkeypatch
    ):
        monkeypatch.setenv("CI", "true")
        _write_config(tmp_path, "touch ran-marker")
        backend = _make("local", tmp_path, harness)
        backend.allow_host_setup = True
        with pytest.raises(HostSetupRefusedError):
            backend._run_setup_steps()
        assert not (tmp_path / "ran-marker").exists()
        assert _step_ran(popen_spy, "ran-marker") == []

    def test_running_in_ci(self):
        assert running_in_ci({"GITLAB_CI": "true"})
        assert not running_in_ci({"CI": "0", "GITHUB_ACTIONS": "false", "GITLAB_CI": ""})
        assert not running_in_ci({})


class TestAllowedRuns:
    @pytest.mark.parametrize("name", BACKEND_NAMES)
    def test_steps_run_with_the_flag(self, tmp_path, harness, capsys, name):
        _write_config(tmp_path, "touch ran-marker")
        _make(name, tmp_path, harness, allow_host_setup=True)._run_setup_steps()
        assert (tmp_path / "ran-marker").exists()
        assert "Running setup steps" in capsys.readouterr().out

    def test_skip_setup_wins_over_the_flag(self, tmp_path, harness, popen_spy, capsys, monkeypatch):
        monkeypatch.setenv("AGENTIC_CI_SKIP_SETUP", "1")
        _write_config(tmp_path, "touch ran-marker")
        _make("local", tmp_path, harness, allow_host_setup=True)._run_setup_steps()
        assert not (tmp_path / "ran-marker").exists()
        assert _step_ran(popen_spy, "ran-marker") == []
        assert capsys.readouterr().out == ""

    def test_failing_step_raises(self, tmp_path, harness):
        _write_config(tmp_path, "exit 3", "touch never-marker")
        backend = _make("local", tmp_path, harness, allow_host_setup=True)
        with pytest.raises(subprocess.CalledProcessError) as exc:
            backend._run_setup_steps()
        assert exc.value.returncode == 3
        # The error names the step, not the repo's command text.
        assert exc.value.cmd == "Setup step 1/2"
        assert "exit 3" not in str(exc.value)
        assert not (tmp_path / "never-marker").exists()

    def test_step_runs_in_the_workdir(self, tmp_path, harness):
        _write_config(tmp_path, "pwd > pwd.txt")
        _make("local", tmp_path, harness, allow_host_setup=True)._run_setup_steps()
        assert (tmp_path / "pwd.txt").read_text().strip() == str(tmp_path)

    def test_deprecation_warning_once_per_backend(self, tmp_path, harness, capsys):
        _write_config(tmp_path, "true", "true", "true")
        backend = _make("local", tmp_path, harness, allow_host_setup=True)
        backend._run_setup_steps()
        backend._run_setup_steps()
        out = capsys.readouterr().out
        assert out.count("deprecated") == 1
        assert "WARNING: host setup (--allow-host-setup) is deprecated" in out
        # A new backend warns again.
        _make("local", tmp_path, harness, allow_host_setup=True)._run_setup_steps()
        assert capsys.readouterr().out.count("deprecated") == 1


class TestStepEnvironment:
    @pytest.fixture()
    def planted(self, tmp_path, monkeypatch):
        for key, value in PLANTED_SECRETS.items():
            monkeypatch.setenv(key, value)
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        (fake_home / ".gitconfig").write_text("[user]\n\tname = planted-home-name\n")
        global_config = tmp_path / "planted-global.gitconfig"
        global_config.write_text("[user]\n\tname = planted-global-name\n")
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        monkeypatch.setenv("PATH", f"/planted/bin:{os.environ['PATH']}")
        workdir = tmp_path / "work"
        workdir.mkdir()
        return workdir

    def test_popen_gets_exactly_the_built_env(self, planted, harness, popen_spy):
        _write_config(planted, "true")
        _make("local", planted, harness, allow_host_setup=True)._run_setup_steps()
        (call,) = [c for c in popen_spy if c[0] == "Popen"]
        env = call[2]["env"]
        home = env["HOME"]
        assert env == {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": home,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        assert call[2]["start_new_session"] is True
        assert call[2]["shell"] is True
        assert call[2]["cwd"] == str(planted)

    def test_step_sees_only_the_built_env(self, planted, harness):
        _write_config(
            planted,
            'env > env.txt; echo "$HOME" > home.txt; touch "$HOME/made-by-step"',
            'test -e "$HOME/made-by-step" && echo shared > shared.txt',
        )
        _make("local", planted, harness, allow_host_setup=True)._run_setup_steps()

        env = dict(
            line.split("=", 1) for line in (planted / "env.txt").read_text().splitlines() if line
        )
        for key in PLANTED_SECRETS:
            assert key not in env
        assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        # Only what the shell itself adds may appear besides the built env.
        extra = set(env) - {
            "PATH",
            "HOME",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM",
        }
        assert extra <= {"PWD", "SHLVL", "_", "OLDPWD"}

        home = Path((planted / "home.txt").read_text().strip())
        assert home != planted.parent / "fake-home"
        assert home.parent == Path(tempfile.gettempdir())
        # One HOME shared by the steps of a call, removed afterwards.
        assert (planted / "shared.txt").read_text().strip() == "shared"
        assert not home.exists()

    def test_global_gitconfig_is_not_read(self, planted, harness):
        _git_init(planted)
        _write_config(
            planted,
            "{ git --version; git config --get user.name; echo rc=$?; } > git.txt 2>&1",
        )
        _make("local", planted, harness, allow_host_setup=True)._run_setup_steps()
        out = (planted / "git.txt").read_text()
        # git ran and found no user.name anywhere.
        assert out.startswith("git version ")
        assert "rc=1" in out
        assert "planted" not in out

    def test_home_removal_failure_is_logged(self, planted, harness, capsys):
        _write_config(planted, "true")
        real_rmtree = shutil.rmtree
        left = []

        def failing_rmtree(path, *args, **kwargs):
            left.append(path)
            raise OSError("boom")

        try:
            with mock.patch.object(backend_module.shutil, "rmtree", side_effect=failing_rmtree):
                _make("local", planted, harness, allow_host_setup=True)._run_setup_steps()
        finally:
            for path in left:
                real_rmtree(path, ignore_errors=True)
        assert len(left) == 1
        out = capsys.readouterr().out
        assert "WARNING: could not remove the temporary HOME of the host setup steps" in out
        assert f"Temporary HOME left behind: {left[0]}" in out
        assert "boom" not in out


class TestProcessGroup:
    def test_background_child_is_killed(self, tmp_path, harness):
        _write_config(tmp_path, "sleep 300 >/dev/null 2>&1 & echo $! > bg.pid")
        _make("local", tmp_path, harness, allow_host_setup=True)._run_setup_steps()
        pid = int((tmp_path / "bg.pid").read_text())
        try:
            assert _process_gone(pid)
        finally:
            _kill_quietly(pid)

    def test_group_is_killed_on_timeout(self, tmp_path, harness, monkeypatch):
        monkeypatch.setattr(backend_module, "HOST_SETUP_TIMEOUT", 3)
        _write_config(tmp_path, "sleep 300 >/dev/null 2>&1 & echo $! > bg.pid; sleep 300")
        backend = _make("local", tmp_path, harness, allow_host_setup=True)
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            backend._run_setup_steps()
        assert exc.value.cmd == "Setup step 1/1"
        assert "sleep" not in str(exc.value)
        assert (tmp_path / "bg.pid").exists()
        pid = int((tmp_path / "bg.pid").read_text())
        try:
            assert _process_gone(pid)
        finally:
            _kill_quietly(pid)

    def test_killpg_ignores_an_empty_group(self, tmp_path, harness):
        _write_config(tmp_path, "true")
        with mock.patch.object(backend_module.os, "killpg", side_effect=ProcessLookupError):
            _make("local", tmp_path, harness, allow_host_setup=True)._run_setup_steps()

    def test_killpg_permission_error_does_not_fail_the_step(self, tmp_path, harness, capsys):
        _write_config(tmp_path, "true")
        with mock.patch.object(
            backend_module.os, "killpg", side_effect=PermissionError("denied")
        ) as killpg:
            _make("local", tmp_path, harness, allow_host_setup=True)._run_setup_steps()
        assert killpg.called
        out = capsys.readouterr().out
        assert "WARNING: could not kill the processes left by a host setup step" in out
        assert "denied" not in out


class TestWorkdirGit:
    WRITE_GIT = (
        "git config user.name step-name && mkdir -p .git/hooks && "
        "printf '#!/bin/sh\\nexit 0\\n' > .git/hooks/pre-commit && "
        "chmod +x .git/hooks/pre-commit"
    )

    def _assert_restored(self, workdir, config_before):
        assert (workdir / ".git" / "config").read_text() == config_before
        assert not (workdir / ".git" / "hooks" / "pre-commit").exists()

    def test_git_control_files_are_restored(self, tmp_path, harness, capsys):
        _git_init(tmp_path)
        config_before = (tmp_path / ".git" / "config").read_text()
        _write_config(tmp_path, self.WRITE_GIT)
        _make("local", tmp_path, harness, allow_host_setup=True)._run_setup_steps()
        self._assert_restored(tmp_path, config_before)
        out = capsys.readouterr().out
        assert "restored: config, hooks" in out

    def test_restored_when_a_step_fails(self, tmp_path, harness):
        _git_init(tmp_path)
        config_before = (tmp_path / ".git" / "config").read_text()
        _write_config(tmp_path, self.WRITE_GIT + " && exit 4")
        backend = _make("local", tmp_path, harness, allow_host_setup=True)
        with pytest.raises(subprocess.CalledProcessError) as exc:
            backend._run_setup_steps()
        # Exit 4 proves the git writes ran before the step failed.
        assert exc.value.returncode == 4
        self._assert_restored(tmp_path, config_before)

    def test_unchanged_git_logs_no_restore(self, tmp_path, harness, capsys):
        _git_init(tmp_path)
        _write_config(tmp_path, "true")
        _make("local", tmp_path, harness, allow_host_setup=True)._run_setup_steps()
        assert "restored:" not in capsys.readouterr().out

    def test_podman_snapshots_the_restored_git(self, tmp_path, harness, monkeypatch):
        # Podman records the host .git after host setup; it must record the
        # restored copy, not what a setup step wrote.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        _git_init(tmp_path)
        config_before = (tmp_path / ".git" / "config").read_bytes()
        _write_config(tmp_path, self.WRITE_GIT)
        backend = PodmanBackend(
            workdir=str(tmp_path),
            image="localhost/x:1",
            harness=harness,
            allow_host_setup=True,
        )
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:1] in (["podman"], ["chown"]):
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            return real_run(cmd, **kwargs)

        with (
            mock.patch.object(backend, "_resolve_sandbox_config"),
            mock.patch.object(backend, "is_running", return_value=False),
            mock.patch("agentic_ci.backends.podman.subprocess.run", side_effect=fake_run),
        ):
            backend.setup()
        assert backend._host_git is not None
        assert backend._host_git.entries["config"].data == config_before
        assert "hooks/pre-commit" not in backend._host_git.entries
        assert (tmp_path / ".git" / "config").read_bytes() == config_before


class TestCli:
    def _main(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["agentic-ci", *argv])
        cli.main()

    @pytest.mark.parametrize("command", ["setup", "run"])
    def test_flag_maps_to_create_backend(self, monkeypatch, tmp_path, command):
        extra = ["prompt"] if command == "run" else []
        with (
            mock.patch("agentic_ci.cli.create_backend") as cb,
            mock.patch("agentic_ci.cli.cmd_setup"),
            mock.patch("agentic_ci.cli.cmd_run"),
        ):
            self._main(
                monkeypatch,
                command,
                *extra,
                "--backend",
                "local",
                "--workdir",
                str(tmp_path),
                "--allow-host-setup",
            )
        assert cb.call_args.kwargs["allow_host_setup"] is True

    @pytest.mark.parametrize("command", ["setup", "run", "stop"])
    def test_no_flag_by_default(self, monkeypatch, tmp_path, command):
        extra = ["prompt"] if command == "run" else []
        with (
            mock.patch("agentic_ci.cli.create_backend") as cb,
            mock.patch("agentic_ci.cli.cmd_setup"),
            mock.patch("agentic_ci.cli.cmd_run"),
            mock.patch("agentic_ci.cli.cmd_stop"),
        ):
            self._main(
                monkeypatch, command, *extra, "--backend", "local", "--workdir", str(tmp_path)
            )
        assert "allow_host_setup" not in cb.call_args.kwargs

    @pytest.mark.parametrize("command", ["setup", "run"])
    def test_ci_refusal_is_a_usage_error(self, monkeypatch, tmp_path, capsys, command):
        monkeypatch.setenv("GITLAB_CI", "true")
        extra = ["prompt"] if command == "run" else []
        with (
            mock.patch("agentic_ci.cli.cmd_setup") as setup,
            mock.patch("agentic_ci.cli.cmd_run") as run,
            pytest.raises(SystemExit) as exc,
        ):
            self._main(
                monkeypatch,
                command,
                *extra,
                "--backend",
                "local",
                "--workdir",
                str(tmp_path),
                "--allow-host-setup",
            )
        assert exc.value.code == 2
        assert "--allow-host-setup is refused in CI" in capsys.readouterr().err
        setup.assert_not_called()
        run.assert_not_called()


def test_backends_accept_the_flag(tmp_path, harness):
    for cls in (LocalBackend, PodmanBackend, OpenShellBackend):
        assert cls(workdir=str(tmp_path), harness=harness).allow_host_setup is False
        assert (
            cls(workdir=str(tmp_path), harness=harness, allow_host_setup=True).allow_host_setup
            is True
        )
