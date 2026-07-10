"""Local (direct execution) backend for agentic-ci."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from agentic_ci import log
from agentic_ci.backend import Backend
from agentic_ci.otel import wait_for_otel_complete

if TYPE_CHECKING:
    from agentic_ci.harness import Harness


class LocalBackend(Backend):
    """Runs an AI agent directly in the local environment.

    No container or sandbox — the agent binary must already be installed
    and accessible on PATH. Useful when agentic-ci is running inside an
    existing CI container (e.g. Prow) where an extra isolation layer is
    unnecessary.
    """

    def __init__(self, workdir=".", extra_env=None, *, harness: Harness):
        super().__init__(workdir=workdir, image=None, harness=harness)
        self._extra_env = extra_env or {}

    def setup(self, otel_port=None):
        log.section("Local backend (direct execution)")

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
    ):
        log.section(f"Executing {self.harness.name} locally")

        env = {
            **os.environ,
            **self.harness.build_local_env(otel_port, otel_rate_file),
            "AGENT_MODEL": model,
            **self._extra_env,
        }

        agent_args = self.harness.build_args(prompt, model, extra_args)

        proc = subprocess.Popen(
            agent_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workdir,
            env=env,
        )

        rc, stream_complete = self._process_stream(proc, streaming)
        if otel_port:
            wait_for_otel_complete(otel_port, agent_proc=proc)
        rc = self._resolve_exit_code(rc, stream_complete)
        return rc
