"""Abstract base class for sandbox backends."""

from __future__ import annotations

import os
import sys
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from agentic_ci import log

if TYPE_CHECKING:
    from agentic_ci.harness import Harness


class Backend(ABC):
    """Base class for sandbox backends.

    Subclasses implement setup() and run() to provide different
    execution environments (OpenShell sandbox, Podman container, etc.).
    """

    def __init__(self, workdir=".", image=None, harness: Harness | None = None):
        self.workdir = os.path.abspath(workdir)
        self.image = image
        self.harness = harness

    @abstractmethod
    def setup(self):
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
    ) -> int:
        """Execute the agent with the given prompt. Returns the exit code."""

    def _process_stream(self, proc, streaming):
        """Read output from proc.stdout through the harness stream processor.

        Returns the exit code, treating stream-complete as success even
        if the process exit code is non-zero.
        """
        stream_complete = False

        if streaming:
            processor = self.harness.create_stream_processor(pid=proc.pid)
            for line in proc.stdout:
                text = line.decode("utf-8", errors="replace")
                if processor.process_line(text):
                    stream_complete = True
                    break
        else:
            for line in proc.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()

        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        rc = proc.returncode

        if stream_complete and rc != 0:
            log.info(f"stream processor detected run complete (rc={rc}), treating as success")
            rc = 0

        return rc

    def _wait_for_otel_flush(self, otel_port):
        """Wait for OTEL metrics to flush after Claude exits."""
        if otel_port:
            time.sleep(7)
