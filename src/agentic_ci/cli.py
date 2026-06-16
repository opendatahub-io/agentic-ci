"""CLI entry point for agentic-ci."""

import argparse
import os
import shutil
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from agentic_ci import log, mlflow, otel, plugins
from agentic_ci.backends import create_backend
from agentic_ci.forge.cli import register_subcommands
from agentic_ci.gates import resolve_gates, validate_gate_env
from agentic_ci.harness import create_harness


def _parse_gate_list(value: str | None) -> list[str]:
    """Split a comma-separated gate list into names."""
    if not value:
        return []
    return [g.strip() for g in value.split(",") if g.strip()]


def cmd_setup(args, backend):
    backend.setup()
    log.section("Setup complete")


def cmd_stop(args, backend):
    backend.stop()


def cmd_mlflow_push(args):
    log.section("Pushing traces to MLflow")
    log.detail("JSONL", args.jsonl)
    log.detail("Endpoint", args.endpoint)
    log.detail("Experiment", args.experiment)

    ok, err = mlflow.push_traces(args.jsonl, args.endpoint, args.experiment, args.token)
    if ok:
        log.info(f"Pushed {ok} trace(s) to MLflow ({err} failed)")
    elif err:
        log.info(f"Failed to push traces to MLflow ({err} errors)")
        sys.exit(1)
    else:
        log.info("No /v1/traces records found in JSONL")


def cmd_install_plugins(args):
    harness_name = args.harness or os.environ.get("AGENT_TOOL", "claude-code")
    if harness_name in ("claude", "claude-code"):
        seed_dir = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR", "")
        if not seed_dir:
            print("ERROR: CLAUDE_CODE_PLUGIN_CACHE_DIR must be set", file=sys.stderr)
            sys.exit(1)
        manifest = Path(args.manifest) if args.manifest else None
        plugins.install_claude_plugins(Path(seed_dir), manifest_path=manifest)
    elif harness_name == "opencode":
        if not args.marketplace_json:
            print("ERROR: --marketplace-json is required for opencode", file=sys.stderr)
            sys.exit(1)
        manifest = Path(args.manifest) if args.manifest else None
        plugins.install_opencode_skills(Path(args.marketplace_json), manifest_path=manifest)
    else:
        print(f"ERROR: unknown harness {harness_name!r}", file=sys.stderr)
        sys.exit(1)


def cmd_run(args, backend, harness):
    pre_gate_names = _parse_gate_list(getattr(args, "pre_gates", None))
    post_gate_names = _parse_gate_list(getattr(args, "post_gates", None))

    all_gates = []
    if pre_gate_names:
        all_gates.extend(resolve_gates(pre_gate_names))
    if post_gate_names:
        all_gates.extend(resolve_gates(post_gate_names))
    if all_gates:
        validate_gate_env(all_gates)

    if pre_gate_names:
        log.section("Running pre-gates")
        pre_specs = resolve_gates(pre_gate_names)
        for gate in pre_specs:
            errors = gate.fn(workdir=args.workdir)
            if errors:
                for err in errors:
                    print(f"Pre-gate {gate.name} blocked: {err}", flush=True)
                sys.exit(0)
            log.info(f"{gate.name}: passed")

    model_env = harness.model_env_var()
    if args.model:
        model = args.model
    elif os.environ.get(model_env):
        model = os.environ[model_env]
    else:
        model = harness.default_model()

    run_dir = tempfile.mkdtemp(prefix="agentic-ci-run.")

    otel_port = None
    otel_log = None
    otel_rate = None
    otel_proc = None
    rc = 1

    try:
        if not args.no_otel and harness.supports_otel:
            log.section("Starting OTEL collector")
            bind_addr = "0.0.0.0" if args.backend == "openshell" else "127.0.0.1"
            otel_proc, otel_port, otel_log, otel_rate = otel.start_collector(
                run_dir, bind_addr=bind_addr
            )
            os.environ["OTEL_RATE_FILE"] = otel_rate
            log.detail("pid", str(otel_proc.pid))
            log.detail("port", str(otel_port))

        backend.setup(otel_port=otel_port)

        log.section(f"Running {harness.name} ({model}) via {args.backend} backend")
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
            otel_proc = None
            print(flush=True)
            log.section(f"Agent exit code: {rc}")
            log.section("Token/Cost Summary (OpenTelemetry)")
            otel.print_summary(otel_log)
        else:
            print(flush=True)
            log.section(f"Agent exit code: {rc}")

        if rc == 0 and post_gate_names:
            log.section("Running post-gates")
            post_specs = resolve_gates(post_gate_names)
            gate_errors: list[str] = []
            for gate in post_specs:
                errors = gate.fn(workdir=args.workdir)
                if errors:
                    gate_errors.extend(errors)
                    for err in errors:
                        print(f"  {gate.name}: FAILED - {err}", file=sys.stderr, flush=True)
                else:
                    log.info(f"{gate.name}: passed")
            if gate_errors:
                rc = 1

        artifact_dir = os.environ.get("GITHUB_WORKSPACE") or os.environ.get("CI_PROJECT_DIR")
        if artifact_dir and otel_log:
            try:
                shutil.copy2(otel_log, artifact_dir)
            except (OSError, FileNotFoundError):
                pass

    except KeyboardInterrupt:
        print(flush=True)
        log.section("Interrupted")
        rc = 130

    finally:
        if otel_proc:
            otel.stop_collector(otel_proc)
            os.environ.pop("OTEL_RATE_FILE", None)
        if not args.keep:
            backend.stop()

    sys.exit(rc)


def main():
    parser = argparse.ArgumentParser(
        prog="agentic-ci",
        description="Run AI coding agents in a sandboxed CI environment",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {version('agentic-ci')}",
    )
    sub = parser.add_subparsers(dest="command")

    # Common arguments shared by all subcommands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--backend",
        choices=["podman", "openshell"],
        default="podman",
        help="Sandbox backend (default: podman)",
    )
    common.add_argument("--workdir", default=".", metavar="PATH", help="Working directory")
    common.add_argument("--image", default=None, metavar="IMAGE", help="Container/sandbox image")
    common.add_argument(
        "--harness",
        choices=["claude-code", "opencode"],
        default="claude-code",
        help="AI agent harness (default: claude-code)",
    )
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

    p_forge = sub.add_parser("forge", help="Git forge (GitLab/GitHub) MR/PR operations")
    register_subcommands(p_forge)

    p_run = sub.add_parser(
        "run", parents=[common], help="Execute a prompt in a sandbox environment"
    )
    p_run.add_argument("prompt", help="Prompt to send to the agent")
    p_run.add_argument(
        "--no-streaming", action="store_true", help="Disable pretty-printed stream output"
    )
    p_run.add_argument("--no-otel", action="store_true", help="Disable OTEL telemetry collection")
    p_run.add_argument(
        "--keep",
        action="store_true",
        help="Keep the sandbox environment running after the run completes",
    )
    p_run.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Agent model (default from harness env var or harness default)",
    )
    p_run.add_argument(
        "--pre-gates",
        default=None,
        metavar="GATES",
        help="Comma-separated list of pre-agent gates to run before the agent",
    )
    p_run.add_argument(
        "--post-gates",
        default=None,
        metavar="GATES",
        help="Comma-separated list of post-agent gates to run after the agent",
    )
    p_push = sub.add_parser(
        "mlflow-push",
        help="Push OTel traces from a JSONL log to an MLflow OTLP endpoint",
    )
    p_push.add_argument("jsonl", help="Path to claude-otel.jsonl file")
    p_push.add_argument(
        "--endpoint",
        default=os.environ.get("MLFLOW_TRACKING_URI"),
        required=not os.environ.get("MLFLOW_TRACKING_URI"),
        metavar="URL",
        help="MLflow tracking URI (env: MLFLOW_TRACKING_URI)",
    )
    p_push.add_argument(
        "--experiment",
        default=os.environ.get("MLFLOW_EXPERIMENT_NAME"),
        required=not os.environ.get("MLFLOW_EXPERIMENT_NAME"),
        metavar="NAME",
        help="MLflow experiment name (env: MLFLOW_EXPERIMENT_NAME)",
    )
    p_push.add_argument(
        "--token",
        default=os.environ.get("MLFLOW_TRACKING_TOKEN"),
        metavar="TOKEN",
        help="MLflow Bearer token (env: MLFLOW_TRACKING_TOKEN)",
    )

    p_install = sub.add_parser(
        "install-plugins",
        help="Install plugins/skills from a marketplace into the image",
    )
    p_install.add_argument(
        "--marketplace-json",
        metavar="PATH",
        help="Path to marketplace.json (required for opencode, optional for claude-code)",
    )
    p_install.add_argument(
        "--harness",
        choices=["claude-code", "opencode"],
        default=None,
        help="Harness to install for (default: from AGENT_TOOL env or claude-code)",
    )
    p_install.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help="Override manifest output path",
    )

    sub.add_parser(
        "enable-plugins",
        help="Filter active plugins based on AGENT_ENABLED_PLUGINS",
    )

    args, extra = parser.parse_known_args()
    if hasattr(args, "prompt"):
        args.extra_args = extra
    else:
        args.extra_args = []

    if args.command == "forge":
        args.func(args)
        return

    if args.command == "mlflow-push":
        cmd_mlflow_push(args)
        return

    if args.command == "install-plugins":
        cmd_install_plugins(args)
        return

    if args.command == "enable-plugins":
        plugins.enable_plugins()
        return

    if args.command not in ("setup", "run", "stop"):
        parser.print_help()
        sys.exit(1)

    harness = create_harness(args.harness)

    log.section(f"Backend: {args.backend}")
    log.detail("Harness", harness.name)
    log.detail("Auth", "API key" if harness.auth_mode == "api-key" else "Vertex AI")
    log.detail("Workdir", os.path.abspath(args.workdir))
    backend = create_backend(
        args.backend,
        harness=harness,
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
        cmd_run(args, backend, harness)


if __name__ == "__main__":
    main()
