"""CLI entry point for agentic-ci."""

import argparse
import os
import shutil
import sys
import tempfile

from agentic_ci import claude, gateway, otel, policy, sandbox


def _ensure_gateway():
    if not gateway.is_running():
        print("--- Starting OpenShell gateway ---", flush=True)
        gateway.start()
    else:
        print("--- OpenShell gateway already running ---", flush=True)


def _ensure_sandbox(args):
    """Create the sandbox and upload credentials if it doesn't exist."""
    if sandbox.exists():
        print("--- Sandbox already exists ---", flush=True)
        return

    policy_path = policy.resolve(
        flag_path=getattr(args, "policy", None),
        workdir=getattr(args, "workdir", "."),
    )
    image = getattr(args, "image", None)

    print(f"--- Creating sandbox (policy: {policy_path}) ---", flush=True)
    sandbox.create(image=image, policy_path=policy_path)

    print("--- Uploading credentials ---", flush=True)
    claude.setup_credentials()


def cmd_setup(args):
    _ensure_gateway()
    _ensure_sandbox(args)
    print("--- Setup complete ---", flush=True)


def cmd_run(args):
    _ensure_gateway()
    _ensure_sandbox(args)

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

    print(f"--- Running Claude ({model}) in sandbox ---", flush=True)
    rc = claude.run(
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
        description="Run Claude Code in a sandboxed CI environment via OpenShell",
    )
    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser("setup", help="Start gateway, create sandbox, upload credentials")
    p_setup.add_argument(
        "--policy", default=None, metavar="PATH", help="Explicit policy file override"
    )
    p_setup.add_argument("--workdir", default=".", metavar="PATH", help="Working directory")
    p_setup.add_argument("--image", default=None, metavar="IMAGE", help="Sandbox base image")

    p_run = sub.add_parser("run", help="Execute a prompt inside the sandbox")
    p_run.add_argument("prompt", help="Prompt to send to Claude")
    p_run.add_argument(
        "--policy", default=None, metavar="PATH", help="Explicit policy file override"
    )
    p_run.add_argument("--workdir", default=".", metavar="PATH", help="Working directory")
    p_run.add_argument("--image", default=None, metavar="IMAGE", help="Sandbox base image")
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

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
