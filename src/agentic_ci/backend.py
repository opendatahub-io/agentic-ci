"""Abstract base class for sandbox backends."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_ci import log
from agentic_ci.config import load_config
from agentic_ci.git import (
    GitControlSnapshot,
    discard_git_dir,
    restore_git_control,
    snapshot_git_control,
)

if TYPE_CHECKING:
    from agentic_ci.harness import Harness

# Environment variables that mark a CI job. ``allow_host_setup`` is refused
# when any of them is set to a value other than ``0`` or ``false``.
CI_ENV_MARKERS = ("CI", "GITLAB_CI", "GITHUB_ACTIONS")

# Seconds one host setup step may run before it is stopped.
HOST_SETUP_TIMEOUT = 600

# The only PATH a host setup step gets.
HOST_SETUP_PATH = "/usr/local/bin:/usr/bin:/bin"


class HostSetupRefusedError(ValueError):
    """``allow_host_setup`` was requested in a CI job, where it is never allowed."""

    def __init__(self) -> None:
        super().__init__(
            "--allow-host-setup is refused in CI (CI, GITLAB_CI or GITHUB_ACTIONS is set); "
            "use a sandbox profile's setup steps instead"
        )


def running_in_ci(env: Mapping[str, str] | None = None) -> bool:
    """Whether *env* (default ``os.environ``) marks a CI job.

    A marker counts when it is set to a non-empty value other than ``0`` or
    ``false`` (any case).
    """
    env = os.environ if env is None else env
    for name in CI_ENV_MARKERS:
        value = env.get(name, "")
        if value and value.lower() not in ("0", "false"):
            return True
    return False


def _kill_process_group(pgid: int) -> None:
    """Kill every process left in the process group *pgid*, if any.

    Best effort: an empty group, or one whose remaining processes cannot be
    signalled (for example a backgrounded ``sudo`` child), is not an error.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        log.info("WARNING: could not kill the processes left by a host setup step")


class Backend(ABC):
    """Base class for sandbox backends.

    Subclasses implement setup() and run() to provide different
    execution environments (OpenShell sandbox, Podman container, etc.).
    """

    collector_bind_address: str = "127.0.0.1"

    def __init__(
        self, workdir=".", image=None, *, harness: Harness, allow_host_setup: bool = False
    ):
        if allow_host_setup and running_in_ci():
            raise HostSetupRefusedError()
        self.workdir = os.path.abspath(workdir)
        self.image = image
        self.harness = harness
        self.verdict_path: Path | None = None
        self.output_file: Path | None = None
        # Host directory for run records (the skill session's ``_run``);
        # None writes none.
        self.run_dir: Path | None = None
        # False skips a sandbox profile's validate commands after the next
        # run (a classifier run, which changes no code). Backends without
        # profile steps ignore it.
        self.validate_after_run = True
        # The skill the agent runs, if known; backends export its installed
        # directory as CLAUDE_SKILL_DIR for harnesses that do not set it.
        self.skill_name: str | None = None
        self._host_git: GitControlSnapshot | None = None
        # Deprecated: run the repo's .agentic-ci/config.yml setup steps on
        # this host (see _run_setup_steps). Never true in CI.
        self.allow_host_setup = allow_host_setup
        self._host_setup_warned = False

    @abstractmethod
    def setup(self, otel_port: int | None = None):
        """Prepare the backend. Idempotent."""

    @abstractmethod
    def stop(self):
        """Tear down the sandbox environment."""

    @abstractmethod
    def run(
        self,
        prompt,
        model,
        streaming=True,
        otel_port=None,
        otel_rate_file=None,
        extra_args=None,
        traceparent=None,
        effort=None,
    ) -> int:
        """Execute the agent with the given prompt. Returns the exit code.

        *effort* is the resolved reasoning effort (``None`` when no effort
        flag is passed). It is exported to the agent as
        ``AGENT_REASONING_EFFORT`` next to ``AGENT_MODEL``; the matching CLI
        flags come from the caller through *extra_args*.
        """

    def _process_stream(self, proc, streaming):
        """Read output from proc.stdout through the harness stream processor.

        Returns ``(rc, stream_complete)`` so callers can insert
        backend-specific steps (e.g. downloading the workdir) before
        the verdict check in :meth:`_resolve_exit_code`.
        """
        stderr_buf = bytearray()

        def _drain_stderr():
            if proc.stderr is None:
                return
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_buf.extend(chunk)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        stream_complete = False

        out_fh = None
        if self.output_file is not None:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            out_fh = open(self.output_file, "w", encoding="utf-8")

        try:
            processor = None
            if streaming:
                processor = self.harness.create_stream_processor(pid=proc.pid)
                for line in proc.stdout:
                    text = line.decode("utf-8", errors="replace")
                    if out_fh is not None:
                        out_fh.write(text)
                        out_fh.flush()
                    if processor.process_line(text):
                        stream_complete = True
                        break
            else:
                for line in proc.stdout:
                    if out_fh is not None:
                        out_fh.write(line.decode("utf-8", errors="replace"))
                        out_fh.flush()
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
        finally:
            if out_fh is not None:
                out_fh.close()

        if stream_complete:
            # Stream processor detected the run is done but the process is
            # still alive.  Give it time to flush the OTEL batch exporter
            # before terminating; without this the root span is often lost.
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Process may exit between wait() timeout and kill(), raising
            # ProcessLookupError -- catch it so cleanup can continue.
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
        stderr_thread.join(timeout=5)
        rc = proc.returncode

        if processor:
            processor.flush_errors()

        if rc != 0 and stderr_buf:
            filtered = self._filter_stderr_noise(stderr_buf)
            if filtered:
                log.section("Agent stderr")
                sys.stderr.buffer.write(filtered)
                sys.stderr.buffer.flush()

        return rc, stream_complete

    def _resolve_exit_code(self, rc, stream_complete):
        """Apply verdict-based exit code override.

        When the stream processor detected a completed run but the
        process exited non-zero (e.g. SIGKILL after the agent finished),
        promote the exit code to 0 if the verdict file is present.
        """
        if stream_complete and rc != 0:
            if self.verdict_path is not None and not self.verdict_path.exists():
                log.info(
                    f"stream completed (rc={rc}) but verdict file "
                    f"{self.verdict_path} missing; keeping original exit code"
                )
            else:
                log.info(f"stream processor detected run complete (rc={rc}), treating as success")
                rc = 0
        return rc

    _STDERR_NOISE = (
        "Performing one time database migration",
        "sqlite-migration:",
        "Database migration complete",
    )

    @classmethod
    def _filter_stderr_noise(cls, buf):
        lines = buf.decode("utf-8", errors="replace").splitlines(keepends=True)
        filtered = [line for line in lines if not any(p in line for p in cls._STDERR_NOISE)]
        return "".join(filtered).encode("utf-8") if filtered else b""

    def _run_setup_steps(self):
        """Run the repo's ``.agentic-ci/config.yml`` setup steps on the host (deprecated).

        Off by default: without ``allow_host_setup`` nothing runs and only the
        number of steps is logged. A sandbox profile's setup steps run inside
        the OpenShell sandbox instead.

        With ``allow_host_setup`` (local CLI use only; refused in CI), each
        step runs in the workdir with an environment built from scratch: a
        fixed ``PATH``, a temporary ``HOME`` removed afterwards, and no global
        or system git config, so no inherited secret or host git setting
        reaches it. Each step runs in its own session and process group,
        which is killed once its shell exits or times out; a process that
        leaves the step's process group (``setsid``, ``setpgid``, or shell job
        control such as ``set -m``) is not killed. The
        workdir's ``.git`` control files are recorded before the first step
        and restored after the last one, also when a step fails. None of this
        is a sandbox: a step can still read every file the user can read.

        ``AGENTIC_CI_SKIP_SETUP=1`` skips this entirely, even when allowed.
        """
        if os.environ.get("AGENTIC_CI_SKIP_SETUP") == "1":
            return

        config = load_config(self.workdir)
        if not self.allow_host_setup:
            if config.setup:
                log.info(
                    f"Host setup is disabled: {len(config.setup)} setup step(s) in "
                    ".agentic-ci/config.yml not run (a sandbox profile's setup steps "
                    "run in the sandbox)"
                )
            return

        # Checked again here, not only in __init__: the attribute is public
        # and the environment can change after construction. Fail closed.
        if running_in_ci():
            raise HostSetupRefusedError()

        if not self._host_setup_warned:
            self._host_setup_warned = True
            log.info(
                "WARNING: host setup (--allow-host-setup) is deprecated: it runs the repo's "
                ".agentic-ci/config.yml setup steps on this host, outside any sandbox; "
                "use a sandbox profile's setup steps instead"
            )
        if not config.setup:
            return

        log.section("Running setup steps")
        git_snapshot = snapshot_git_control(Path(self.workdir))
        home = tempfile.mkdtemp(prefix="agentic-ci-setup-home-")
        env = {
            "PATH": HOST_SETUP_PATH,
            "HOME": home,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        try:
            for index, step in enumerate(config.setup, start=1):
                label = f"Setup step {index}/{len(config.setup)}"
                log.detail(label, f"{step.name}: {step.run}")
                self._run_host_setup_step(step.run, env, label=label)
        finally:
            try:
                shutil.rmtree(home)
            except OSError:
                log.info("WARNING: could not remove the temporary HOME of the host setup steps")
                log.detail("Temporary HOME left behind", home)
            changed = restore_git_control(git_snapshot)
            if changed:
                log.info(
                    "WARNING: host setup steps changed the workdir's git control files; "
                    f"restored: {', '.join(changed)}"
                )

    def _run_host_setup_step(self, command: str, env: dict[str, str], *, label: str) -> None:
        """Run one host setup step and kill its process group afterwards.

        Raises :class:`subprocess.CalledProcessError` when the step fails and
        :class:`subprocess.TimeoutExpired` when it runs too long, like
        ``subprocess.run(..., check=True, timeout=...)``. Both carry *label*
        as their command instead of the repo's command text, which is logged
        only through ``log.detail``.
        """
        # shell=True: the repo controls the command either way. The clean env
        # limits accidental exposure; it is not a sandbox.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=self.workdir,
            env=env,
            start_new_session=True,
        )
        try:
            returncode = proc.wait(timeout=HOST_SETUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(label, HOST_SETUP_TIMEOUT) from None
        finally:
            # The shell leads the group (start_new_session), so its pid is the
            # group id; this also stops a shell that is still running.
            _kill_process_group(proc.pid)
            proc.wait()
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, label)

    def _snapshot_host_git(self):
        """Record the workdir's git control files before the agent can write them.

        Sandboxed backends call this while only the host can write the
        workdir, and :meth:`_restore_host_git` once the agent's changes have
        landed on the host. Host-side git (``git commit --amend``,
        ``git push``) then runs with the host's own ``.git/config``, hooks and
        attributes instead of whatever the agent configured, while the
        agent's commits are kept. A snapshot that already exists is kept, so a
        snapshot is never taken while the agent can still write the workdir.
        """
        if self._host_git is None:
            self._host_git = snapshot_git_control(Path(self.workdir))

    def _restore_host_git(self, *, release=False):
        """Put back the git control files recorded by :meth:`_snapshot_host_git`.

        With *release*, the snapshot is dropped afterwards; pass it once the
        agent can no longer write the workdir, so the next run snapshots the
        host state again.
        """
        snapshot = self._host_git
        if snapshot is None:
            return
        try:
            restore_git_control(snapshot)
        finally:
            if release:
                self._host_git = None

    def _discard_host_git(self):
        """Take the workdir's ``.git`` away from host git instead of restoring it.

        For when the agent may still be able to write the workdir, so a
        restore could be undone behind the host's back. Drops the snapshot.
        """
        if self._host_git is None:
            return
        self._host_git = None
        discard_git_dir(Path(self.workdir))

    def _wait_for_otel_flush(self, otel_port):
        """Wait for OTEL metrics to flush after the agent stream ends.

        For containerized backends (OpenShell), _process_stream terminates
        the local exec wrapper, not the remote Claude Code process.  The
        OTEL batch exporter inside the container may still be flushing to
        the host collector, so we give it time.
        """
        if otel_port:
            time.sleep(7)
