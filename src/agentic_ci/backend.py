"""Abstract base class for sandbox backends."""

from __future__ import annotations

import os
import sys
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_ci import log

if TYPE_CHECKING:
    from agentic_ci.harness import Harness


class Backend(ABC):
    """Base class for sandbox backends.

    Subclasses implement setup() and run() to provide different
    execution environments (OpenShell sandbox, Podman container, etc.).
    """

    def __init__(self, workdir=".", image=None, *, harness: Harness):
        self.workdir = os.path.abspath(workdir)
        self.image = image
        self.harness = harness
        self.verdict_path: Path | None = None

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
    ) -> int:
        """Execute the agent with the given prompt. Returns the exit code."""

    def _process_stream(self, proc, streaming):
        """Read output from proc.stdout through the harness stream processor.

        Returns the exit code, treating stream-complete as success even
        if the process exit code is non-zero.
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

        processor = None
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
        stderr_thread.join(timeout=5)
        rc = proc.returncode

        if processor:
            processor.flush_errors()

        if stream_complete and rc != 0:
            if self.verdict_path is not None and not self.verdict_path.exists():
                log.info(
                    f"stream completed (rc={rc}) but verdict file "
                    f"{self.verdict_path} missing; keeping original exit code"
                )
            else:
                log.info(f"stream processor detected run complete (rc={rc}), treating as success")
                rc = 0

        if rc != 0 and stderr_buf:
            filtered = self._filter_stderr_noise(stderr_buf)
            if filtered:
                log.section("Agent stderr")
                sys.stderr.buffer.write(filtered)
                sys.stderr.buffer.flush()

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

    def _wait_for_otel_flush(self, otel_port):
        """Wait for OTEL metrics to flush after Claude exits."""
        if otel_port:
            time.sleep(7)
