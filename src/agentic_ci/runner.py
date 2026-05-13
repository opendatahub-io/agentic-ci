"""Run Claude Code with OTEL telemetry and streaming output.

In-container (or host-direct) entry point. Starts an OTEL collector,
runs Claude with stream-json output, displays human-readable progress,
and prints a token/cost summary.

When run as root, re-execs itself as a non-root user.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from agentic_ci.otel import print_summary, start_collector, stop_collector
from agentic_ci.stream import StreamProcessor


def run(
    prompt: str,
    workdir: str = ".",
    *,
    model: str | None = None,
    user: str = "claude-ci",
    extra_args: list[str] | None = None,
    streaming: bool = True,
    otel: bool = True,
) -> int:
    """Run Claude Code with telemetry and streaming output.

    Returns the exit code (0 for success).
    """
    if extra_args is None:
        extra_args = []

    if os.getuid() == 0:
        os.execvp(
            "runuser",
            [
                "runuser", "-u", user, "--",
                sys.executable, "-m", "agentic_ci.runner",
                prompt, workdir,
                *(["--model", model] if model else []),
                *([] if streaming else ["--no-streaming"]),
                *([] if otel else ["--no-otel"]),
                *(["--"] + extra_args if extra_args else []),
            ],
        )

    if model is None:
        model = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")

    os.environ["PATH"] = os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", "")

    print("--- Preflight checks ---", flush=True)
    subprocess.run(["claude", "--version"], check=True)

    os.chdir(workdir)

    workspace = os.environ.get("WORKSPACE_DIR")
    if workspace:
        run_dir = os.path.join(workspace, "_run")
    else:
        import tempfile
        run_dir = tempfile.mkdtemp(prefix="agentic-ci-run.")
    os.makedirs(run_dir, exist_ok=True)

    otel_log = os.path.join(run_dir, "claude-otel.jsonl")
    otel_rate = os.path.join(run_dir, "claude-otel-rate.json")
    stderr_log = os.path.join(run_dir, "claude-stderr.log")
    stream_capture = os.environ.get(
        "STREAM_CAPTURE_FILE",
        os.path.join(run_dir, "claude-stream-capture.jsonl"),
    )

    collector_proc = None
    otel_port = None

    if otel:
        try:
            collector_proc, otel_port, otel_log, otel_rate = start_collector(run_dir)
            print(
                f"--- OTEL collector started (pid {collector_proc.pid}, port {otel_port}) ---",
                flush=True,
            )
        except RuntimeError as exc:
            print(f"Warning: OTEL collector failed to start: {exc}", file=sys.stderr)
            otel = False

    if otel and otel_port:
        os.environ["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        os.environ["OTEL_METRICS_EXPORTER"] = "otlp"
        os.environ["OTEL_LOGS_EXPORTER"] = "otlp"
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/json"
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{otel_port}"
        os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = "10000"
        os.environ["OTEL_RATE_FILE"] = otel_rate

    with open(stderr_log, "w") as stderr_f, open(stream_capture, "w") as capture_f:
        claude_proc = subprocess.Popen(
            [
                "claude", "-p", prompt,
                "--model", model,
                "--permission-mode", "bypassPermissions",
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                *extra_args,
            ],
            stdout=subprocess.PIPE,
            stderr=stderr_f,
        )

        stream_complete = False
        if streaming:
            processor = StreamProcessor(claude_pid=claude_proc.pid)
            for line in claude_proc.stdout:
                text = line.decode("utf-8", errors="replace")
                capture_f.write(text)
                capture_f.flush()
                if processor.process_line(text):
                    stream_complete = True
                    break
        else:
            for line in claude_proc.stdout:
                capture_f.write(line.decode("utf-8", errors="replace"))

    try:
        claude_proc.kill()
    except OSError:
        pass
    claude_proc.wait()
    rc = claude_proc.returncode

    if stream_complete and rc != 0:
        print(
            f"--- stream processor detected run complete (claude rc={rc}), treating as success ---",
            flush=True,
        )
        rc = 0

    if otel:
        time.sleep(7)

    if collector_proc:
        stop_collector(collector_proc)

    print(f"--- Claude exit code: {rc} ---", flush=True)
    print("--- stderr log ---", flush=True)
    with open(stderr_log) as f:
        sys.stderr.write(f.read())

    if otel:
        print("\n--- OTEL Token/Cost Summary ---", flush=True)
        print_summary(otel_log)

    artifact_dir = os.environ.get("GITHUB_WORKSPACE") or os.environ.get("CI_PROJECT_DIR")
    if artifact_dir:
        for src in [otel_log, stderr_log]:
            try:
                shutil.copy2(src, artifact_dir)
            except (OSError, FileNotFoundError):
                pass

    return rc


def main(args=None):
    import argparse

    parser = argparse.ArgumentParser(description="Run Claude Code in CI with telemetry")
    parser.add_argument("prompt", help="Prompt to send to Claude")
    parser.add_argument("workdir", nargs="?", default=".", help="Working directory")
    parser.add_argument("--model", default=None, help="Claude model")
    parser.add_argument("--no-streaming", action="store_true", help="Disable streaming output")
    parser.add_argument("--no-otel", action="store_true", help="Disable OTEL telemetry")
    parsed, extra = parser.parse_known_args(args)

    sys.exit(run(
        parsed.prompt,
        parsed.workdir,
        model=parsed.model,
        extra_args=extra,
        streaming=not parsed.no_streaming,
        otel=not parsed.no_otel,
    ))


if __name__ == "__main__":
    main()
