"""CLI entry point for agentic-ci."""

import argparse
import os
import shutil
import sys
import tempfile

from agentic_ci import otel
from agentic_ci.backends import create_backend


def cmd_setup(args, backend):
    backend.setup()
    print("--- Setup complete ---", flush=True)


def cmd_stop(args, backend):
    backend.stop()


def cmd_run(args, backend):
    backend.setup()

    model = args.model or os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")

    run_dir = tempfile.mkdtemp(prefix="agentic-ci-run.")

    otel_port = None
    otel_log = None
    otel_rate = None
    otel_proc = None

    if not args.no_otel:
        print("--- Starting OTEL collector ---", flush=True)
        otel_proc, otel_port, otel_log, otel_rate = otel.start_collector(run_dir)
        print(
            f"--- OTEL collector started (pid {otel_proc.pid}, port {otel_port}) ---",
            flush=True,
        )

    print(f"--- Running Claude ({model}) via {args.backend} backend ---", flush=True)
    rc = backend.run(
        prompt=args.prompt,
        model=model,
        otel_port=otel_port,
        otel_rate_file=otel_rate,
        extra_args=args.extra_args,
        streaming=not args.no_streaming,
    )

    if otel_proc:
        otel.stop_collector(otel_proc)
        print(f"\n--- Claude exit code: {rc} ---", flush=True)
        print("--- OTEL Token/Cost Summary ---", flush=True)
        otel.print_summary(otel_log)
    else:
        print(f"\n--- Claude exit code: {rc} ---", flush=True)

    artifact_dir = os.environ.get("GITHUB_WORKSPACE") or os.environ.get("CI_PROJECT_DIR")
    if artifact_dir and otel_log:
        try:
            shutil.copy2(otel_log, artifact_dir)
        except (OSError, FileNotFoundError):
            pass

    sys.exit(rc)


def main():
    parser = argparse.ArgumentParser(
        prog="agentic-ci",
        description="Run Claude Code in a sandboxed CI environment",
    )
    parser.add_argument(
        "--backend",
        choices=["podman", "openshell"],
        default="podman",
        help="Sandbox backend (default: podman)",
    )

    sub = parser.add_subparsers(dest="command")

    # Common arguments shared by both subcommands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workdir", default=".", metavar="PATH", help="Working directory")
    common.add_argument("--image", default=None, metavar="IMAGE", help="Container/sandbox image")
    common.add_argument(
        "--policy",
        default=None,
        metavar="PATH",
        help="Policy file override (openshell backend only)",
    )
    common.add_argument(
        "--timeout",
        type=int,
        default=1200,
        metavar="SECS",
        help="Container timeout in seconds (podman backend only, default: 1200)",
    )

    sub.add_parser("setup", parents=[common], help="Prepare the AI agent sandbox environment")
    sub.add_parser("stop", parents=[common], help="Tear down the sandbox environment")

    p_run = sub.add_parser(
        "run", parents=[common], help="Execute a prompt in a sandbox environment"
    )
    p_run.add_argument("prompt", help="Prompt to send to Claude")
    p_run.add_argument(
        "--no-streaming", action="store_true", help="Disable pretty-printed stream output"
    )
    p_run.add_argument("--no-otel", action="store_true", help="Disable OTEL telemetry collection")
    p_run.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Claude model (default: $CLAUDE_MODEL or claude-opus-4-6)",
    )

    args, extra = parser.parse_known_args()
    if hasattr(args, "prompt"):
        args.extra_args = extra
    else:
        args.extra_args = []

    if args.command not in ("setup", "run", "stop"):
        parser.print_help()
        sys.exit(1)

    backend = create_backend(
        args.backend,
        workdir=args.workdir,
        image=args.image,
        policy=args.policy,
        timeout=args.timeout,
    )

    if args.command == "setup":
        cmd_setup(args, backend)
    elif args.command == "stop":
        cmd_stop(args, backend)
    elif args.command == "run":
        cmd_run(args, backend)


if __name__ == "__main__":
    main()
