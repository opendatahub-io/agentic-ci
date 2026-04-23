"""Run Claude Code in CI with streaming output and telemetry.

Starts an OTEL collector, runs Claude with stream-json output,
displays human-readable progress, and prints a token/cost summary.

When run as root, re-execs itself as a non-root user.
"""

import os
import signal
import subprocess
import sys
import time

from agentic_ci.otel_collector import main as collector_main
from agentic_ci.otel_summary import print_summary
from agentic_ci.stream import StreamProcessor


def run(prompt, workdir=".", model=None, user="claude-ci", extra_args=None):
    """Run Claude Code with telemetry and streaming output.

    extra_args are passed directly to the claude CLI (e.g. --plugin-dir).
    Returns the exit code (0 for success).
    """
    if extra_args is None:
        extra_args = []

    if os.getuid() == 0:
        os.execvp("runuser", [
            "runuser", "-u", user, "--",
            sys.executable, "-m", "agentic_ci.runner",
            prompt, workdir,
            *(["--model", model] if model else []),
            *(["--"] + extra_args if extra_args else []),
        ])

    if model is None:
        model = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")

    os.environ["PATH"] = os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", "")

    print("--- Preflight checks ---", flush=True)
    subprocess.run(["claude", "--version"], check=True)

    os.chdir(workdir)

    workspace = os.environ.get("WORKSPACE_DIR")
    if workspace:
        run_tmp = os.path.join(workspace, "_run")
    else:
        import tempfile
        run_tmp = tempfile.mkdtemp(prefix="agentic-ci-run.")
    os.makedirs(run_tmp, exist_ok=True)

    otel_log = os.path.join(run_tmp, "claude-otel.jsonl")
    otel_rate = os.path.join(run_tmp, "claude-otel-rate.json")
    otel_port_file = os.path.join(run_tmp, "otel-port")
    stderr_log = os.path.join(run_tmp, "claude-stderr.log")
    stream_capture = os.environ.get(
        "STREAM_CAPTURE_FILE",
        os.path.join(run_tmp, "claude-stream-capture.jsonl"),
    )

    for f in [otel_log, otel_port_file]:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass

    # Start OTEL collector with OS-assigned port
    collector_env = {
        **os.environ,
        "OTEL_LOG_FILE": otel_log,
        "OTEL_RATE_FILE": otel_rate,
        "OTEL_COLLECTOR_PORT": "0",
        "OTEL_PORT_FILE": otel_port_file,
    }
    collector_proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_ci.otel_collector"],
        env=collector_env,
    )

    # Wait for collector to write its port
    for _ in range(50):
        if os.path.exists(otel_port_file):
            break
        time.sleep(0.1)
    else:
        print("Error: OTEL collector did not write port file", file=sys.stderr)
        collector_proc.kill()
        return 1

    with open(otel_port_file) as f:
        otel_port = f.read().strip()
    print(f"--- OTEL collector started (pid {collector_proc.pid}, port {otel_port}) ---",
          flush=True)

    # Configure Claude to export OTEL data
    os.environ["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
    os.environ["OTEL_METRICS_EXPORTER"] = "otlp"
    os.environ["OTEL_LOGS_EXPORTER"] = "otlp"
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/json"
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{otel_port}"
    os.environ["OTEL_METRIC_EXPORT_INTERVAL"] = "10000"
    os.environ["OTEL_RATE_FILE"] = otel_rate

    # Run Claude
    with open(stderr_log, "w") as stderr_f, open(stream_capture, "w") as capture_f:
        claude_proc = subprocess.Popen(
            [
                "claude", "-p", prompt,
                "--model", model,
                "--dangerously-skip-permissions",
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                *extra_args,
            ],
            stdout=subprocess.PIPE,
            stderr=stderr_f,
        )

        processor = StreamProcessor(claude_pid=claude_proc.pid)
        stream_complete = False

        for line in claude_proc.stdout:
            text = line.decode("utf-8", errors="replace")
            capture_f.write(text)
            capture_f.flush()
            if processor.process_line(text):
                stream_complete = True
                break

    # Ensure Claude is terminated
    try:
        claude_proc.kill()
    except OSError:
        pass
    claude_proc.wait()
    rc = claude_proc.returncode

    # SIGTERM produces 143 (128+15). Treat as success when stream detected completion.
    if stream_complete and rc != 0:
        print(f"--- stream processor detected run complete (claude rc={rc}), treating as success ---",
              flush=True)
        rc = 0

    # Wait for Claude's final OTEL flush
    time.sleep(7)

    # Stop OTEL collector
    collector_proc.terminate()
    try:
        collector_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        collector_proc.kill()
        collector_proc.wait()

    print(f"--- Claude exit code: {rc} ---", flush=True)
    print("--- stderr log ---", flush=True)
    with open(stderr_log) as f:
        sys.stderr.write(f.read())

    print("\n--- OTEL Token/Cost Summary ---", flush=True)
    print_summary(otel_log)

    # Copy artifacts for CI upload
    artifact_dir = os.environ.get("GITHUB_WORKSPACE") or os.environ.get("CI_PROJECT_DIR")
    if artifact_dir:
        import shutil
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
    parser.add_argument("workdir", nargs="?", default=".",
                        help="Working directory (default: .)")
    parser.add_argument("--model", default=None,
                        help="Claude model (default: $CLAUDE_MODEL or claude-opus-4-6)")
    parsed, extra = parser.parse_known_args(args)

    sys.exit(run(parsed.prompt, parsed.workdir, model=parsed.model, extra_args=extra))


if __name__ == "__main__":
    main()
