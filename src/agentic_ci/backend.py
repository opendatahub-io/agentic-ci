"""Abstract base class for sandbox backends."""

import os
import time
from abc import ABC, abstractmethod

from agentic_ci.stream import StreamProcessor


class Backend(ABC):
    """Base class for sandbox backends.

    Subclasses implement setup() and run() to provide different
    execution environments (OpenShell sandbox, Podman container, etc.).
    """

    def __init__(self, workdir=".", image=None):
        self.workdir = os.path.abspath(workdir)
        self.image = image

    @abstractmethod
    def setup(self):
        """Prepare the backend for running Claude. Idempotent."""

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
        """Execute Claude with the given prompt. Returns the exit code."""

    @staticmethod
    def _build_claude_args(prompt, model, extra_args=None):
        """Build the Claude CLI argument list."""
        args = [
            "claude",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "-p",
            prompt,
        ]
        if extra_args:
            args.extend(extra_args)
        return args

    def _process_stream(self, proc, streaming):
        """Read stream-json from proc.stdout through StreamProcessor.

        Returns the exit code, treating stream-complete as success even
        if the process exit code is non-zero.
        """
        stream_complete = False

        if streaming:
            processor = StreamProcessor(claude_pid=proc.pid)
            for line in proc.stdout:
                text = line.decode("utf-8", errors="replace")
                if processor.process_line(text):
                    stream_complete = True
                    break
        else:
            proc.stdout.read()

        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        rc = proc.returncode

        if stream_complete and rc != 0:
            print(
                f"--- stream processor detected run complete (rc={rc}), treating as success ---",
                flush=True,
            )
            rc = 0

        return rc

    def _wait_for_otel_flush(self, otel_port):
        """Wait for OTEL metrics to flush after Claude exits."""
        if otel_port:
            time.sleep(7)
