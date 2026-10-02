"""Local (direct execution) backend for agentic-ci."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
from typing import TYPE_CHECKING

from agentic_ci import log
from agentic_ci.backend import Backend
from agentic_ci.harness import AGENT_EFFORT_ENV_VAR, SKILL_DIR_ENV_VAR
from agentic_ci.plugins import find_skill_dir, is_skill_name

if TYPE_CHECKING:
    from agentic_ci.harness import Harness


class LocalBackend(Backend):
    """Runs an AI agent directly in the local environment.

    No container or sandbox — the agent binary must already be installed
    and accessible on PATH. Useful when agentic-ci is running inside an
    existing CI container (e.g. Prow) where an extra isolation layer is
    unnecessary.

    The agent runs as the host user with the host environment, so there is
    no sandbox boundary to protect and the workdir's ``.git`` is not
    restored after the run, unlike the Podman and OpenShell backends.
    """

    def __init__(
        self, workdir=".", extra_env=None, *, harness: Harness, allow_host_setup: bool = False
    ):
        super().__init__(
            workdir=workdir, image=None, harness=harness, allow_host_setup=allow_host_setup
        )
        self._extra_env = extra_env or {}

    def setup(self, otel_port=None):
        env = {**os.environ, **self._extra_env}
        self.harness.validate_credentials(env, allow_auth_file=True)
        log.section("Local backend (direct execution)")
        self._run_setup_steps()

    def stop(self):
        pass

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
    ):
        log.section(f"Executing {self.harness.name} locally")

        base_env = {**os.environ, **self._extra_env}
        env = {
            **base_env,
            **self.harness.build_local_env(
                otel_port,
                otel_rate_file,
                traceparent=traceparent,
                env=base_env,
            ),
            "AGENT_MODEL": model,
        }
        # The host env may carry a stale AGENT_REASONING_EFFORT (e.g. when
        # agentic-ci runs inside another agent); export only this run's effort.
        env.pop(AGENT_EFFORT_ENV_VAR, None)
        if effort is not None:
            env[AGENT_EFFORT_ENV_VAR] = effort
        self._set_skill_dir(env)
        otel_endpoint = f"http://127.0.0.1:{otel_port}" if otel_port else None
        agent_args = self.harness.build_args(prompt, model, extra_args, otel_endpoint=otel_endpoint)

        proc = subprocess.Popen(
            agent_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workdir,
            env=env,
        )

        rc, stream_complete = self._process_stream(proc, streaming)
        rc = self._resolve_exit_code(rc, stream_complete)
        self._wait_for_otel_flush(otel_port)
        return rc

    def _set_skill_dir(self, env: dict[str, str]) -> None:
        """Set ``CLAUDE_SKILL_DIR`` in *env* to the directory of the skill this run executes.

        The host env may carry a value for another skill (when agentic-ci
        runs inside another agent, say), so it is replaced, or dropped when
        the skill is not found. Left alone without a skill name, for a
        harness that sets the variable itself (Claude Code), and when the
        caller's ``extra_env`` sets it.
        """
        if (
            not is_skill_name(self.skill_name)
            or self.harness.sets_skill_dir
            or SKILL_DIR_ENV_VAR in self._extra_env
        ):
            return
        env.pop(SKILL_DIR_ENV_VAR, None)
        # The lookup's warnings (a failed ``codex plugin list`` with its
        # stderr, say) stay out of the job log, as they do on the other
        # backends, and so does the path.
        with contextlib.redirect_stdout(io.StringIO()):
            skill_dir = find_skill_dir(self.skill_name, env)
        if skill_dir is None:
            log.detail(SKILL_DIR_ENV_VAR, "not set (skill directory not found)")
            return
        env[SKILL_DIR_ENV_VAR] = str(skill_dir)
        log.detail(SKILL_DIR_ENV_VAR, "set to the installed skill directory")
