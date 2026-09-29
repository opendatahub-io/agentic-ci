"""E2E driver for sandbox-profile egress on the OpenShell backend.

``tests/e2e/e2e-openshell-profile.sh`` probes the network from inside the
sandbox; this driver does the parts that are Python API only. It builds an
:class:`~agentic_ci.backends.openshell.OpenShellBackend` for the Codex
harness (OpenAI key) or the Claude Code harness (Anthropic API key),
optionally with a central sandbox profile, and then either creates the
sandbox (``setup``) or switches the setup shim's egress (``phase``). A
switch also kills the processes earlier execs left running and detaches the
credential provider (openai, api-key or vertex) for the setup and validate
phases (attaches it for agent).

``otlp-run`` checks Codex telemetry through the sandbox: it starts
agentic-ci's OTLP collector, creates the sandbox with the collector rule,
switches to the setup phase and back to the agent phase, and runs Codex
through the harness (the env script, ``codex login`` and the OTLP exporter
flags) against a fake Responses API that the shell script serves on
``host.openshell.internal:<mock-port>``. It prints ``OTLP_*`` lines that
describe what reached the collector.

With toolchains in the profile, ``setup`` also provisions them (real
downloads on the host); ``--run-dir`` receives ``toolchains.json`` and
``--env-out`` the toolchain lines the env script exports. ``agent-exec``
sets the sandbox up (a reuse, so provisioning skips what the sandbox has),
writes the env script and runs ``--agent-script`` under ``codex sandbox``
through it, the way the agent itself is started, in a ``bash -lc`` login
shell like the one codex's shell tool gives the agent's commands.

No LLM is called: the credential provider needs a key to be created, and the
shell script passes a fake one.

Usage::

    python3 tests/e2e/openshell_profile_driver.py setup --image IMG --workdir DIR \\
        [--harness {codex,claude-code}] [--profile-json '{"egress": ["npm", "goproxy"]}']
    python3 tests/e2e/openshell_profile_driver.py phase {setup,validate,agent} \\
        --image IMG --workdir DIR [--harness {codex,claude-code}] \\
        --profile-json '{"egress": ["npm", "goproxy"]}'
    python3 tests/e2e/openshell_profile_driver.py otlp-run --image IMG --workdir DIR \\
        --profile-json '{"egress": ["npm"]}' --mock-port PORT --stall-seconds SECONDS
    python3 tests/e2e/openshell_profile_driver.py agent-exec --image IMG --workdir DIR \\
        --profile-json '{"toolchains": {"go": "auto"}}' --agent-script 'go version'

The Claude Code harness selects api-key auth only when ANTHROPIC_API_KEY is
set in the environment, and Vertex auth when neither it nor
CLAUDE_CODE_OAUTH_TOKEN is.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from agentic_ci import otel
from agentic_ci.backends.openshell import OpenShellBackend, sandbox
from agentic_ci.harness import ClaudeCodeHarness, CodexHarness, Harness
from agentic_ci.sandbox_profile import parse_profile

_HARNESSES: dict[str, type[Harness]] = {"codex": CodexHarness, "claude-code": ClaudeCodeHarness}

# The model name the otlp-run command gives Codex; the fake Responses API
# accepts any, and the collector must report it back.
OTLP_MODEL = "e2e-mock-model"


def _backend(args: argparse.Namespace, policy: str | None = None) -> OpenShellBackend:
    profile = None
    if args.profile_json:
        profile = parse_profile(json.loads(args.profile_json), source="central").profile
    backend = OpenShellBackend(
        workdir=str(args.workdir),
        image=args.image,
        policy=policy,
        harness=_HARNESSES[args.harness](),
        sandbox_profile=profile,
    )
    backend.run_dir = args.run_dir
    return backend


def _setup(backend: OpenShellBackend, args: argparse.Namespace) -> None:
    """``backend.setup()``, then write the toolchain env lines to ``--env-out``."""
    backend.setup()
    if args.env_out is not None:
        lines = backend.toolchain_env.script_lines()
        args.env_out.write_text("".join(f"{line}\n" for line in lines))


def _agent_exec(backend: OpenShellBackend, args: argparse.Namespace) -> int:
    """Run ``--agent-script`` as ``bash -lc`` under ``codex sandbox`` through the env script."""
    _setup(backend, args)
    backend._write_env_script("e2e-model")
    command = backend._agent_command(
        f"/sandbox/{args.workdir.name}",
        [
            "codex",
            "sandbox",
            "-c",
            'sandbox_mode="danger-full-access"',
            "--",
            # A login shell, as codex's shell tool runs the agent's commands.
            "bash",
            "-lc",
            args.agent_script,
        ],
    )
    result = subprocess.run(
        [
            "openshell",
            "sandbox",
            "exec",
            "--name",
            sandbox.SANDBOX_NAME,
            "--no-tty",
            "--",
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    return result.returncode


def _mock_provider_args(mock_port: int) -> list[str]:
    """Codex ``-c`` flags that point it at the fake Responses API on the host alias."""
    base_url = f"http://host.openshell.internal:{mock_port}/v1"
    provider = f'{{name="agentic-ci e2e mock", base_url="{base_url}", wire_api="responses"}}'
    return ["-c", 'model_provider="e2emock"', "-c", f"model_providers.e2emock={provider}"]


def _otlp_run(args: argparse.Namespace) -> int:
    """Run Codex once through the harness with OTLP on; print what the collector got."""
    run_dir = tempfile.mkdtemp(prefix="agentic-ci-e2e-otlp.")
    # The fake Responses API listens on the host; only the agent binaries may
    # reach it, like the collector.
    policy_file = Path(run_dir) / "mock-policy.yml"
    policy_file.write_text(
        f"endpoints:\n  - 'host.openshell.internal:{args.mock_port}:read-write'\n"
    )
    backend = _backend(args, policy=str(policy_file))
    proc, port, log_file, rate_file = otel.start_collector(
        run_dir, bind_addr=backend.collector_bind_address
    )
    os.environ["OTEL_RATE_FILE"] = rate_file
    try:
        backend.setup(otel_port=port)
        # Setup, then agent: the collector rule is parked and restored.
        backend._set_egress_phase("setup")
        backend._set_egress_phase("agent")
        start = time.time()
        rc = backend.run(
            prompt="Reply with only the word pong.",
            model=OTLP_MODEL,
            otel_port=port,
            otel_rate_file=rate_file,
            extra_args=_mock_provider_args(args.mock_port),
        )
        seconds = time.time() - start
    finally:
        otel.stop_collector(proc)
    records = []
    if os.path.exists(log_file):
        with open(log_file) as fh:
            records = [json.loads(line) for line in fh if line.strip()]
    logs = [r for r in records if "/v1/logs" in r.get("path", "")]
    late = [
        r for r in logs if datetime.fromisoformat(r["ts"]).timestamp() >= start + args.stall_seconds
    ]
    token_totals, _, api_requests, _ = otel.parse_metrics(records)
    models = ",".join(sorted({model for model, _ in token_totals})) or "-"
    tokens = sum(token_totals.values())
    # Floor, not round: a run of 149.5 s must not pass a 150 s stall check.
    print(f"OTLP_RUN rc={rc} seconds={int(seconds)} collector_port={port}", flush=True)
    print(f"OTLP_RECORDS {len(records)} logs={len(logs)} logs_after_stall={len(late)}")
    print(f"OTLP_RESPONSES {len(api_requests)} models={models} tokens={tokens:.0f}")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E driver for sandbox-profile egress.")
    parser.add_argument("command", choices=["setup", "phase", "otlp-run", "agent-exec"])
    parser.add_argument("phase", nargs="?", choices=["setup", "validate", "agent"])
    parser.add_argument("--image", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--harness", choices=sorted(_HARNESSES), default="codex")
    parser.add_argument("--profile-json", default=None)
    parser.add_argument("--mock-port", type=int, default=0)
    parser.add_argument("--stall-seconds", type=int, default=0)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--env-out", type=Path, default=None)
    parser.add_argument("--agent-script", default="")
    args = parser.parse_args()

    if args.command == "otlp-run":
        if not args.mock_port:
            parser.error("otlp-run needs --mock-port")
        rc = _otlp_run(args)
        if rc != 0:
            print(f"DRIVER_FAIL otlp-run rc={rc}", flush=True)
            return 1
        print("DRIVER_OK otlp-run", flush=True)
        return 0
    backend = _backend(args)
    if args.command == "agent-exec":
        rc = _agent_exec(backend, args)
        print(f"DRIVER_{'OK' if rc == 0 else 'FAIL'} agent-exec rc={rc}", flush=True)
        return 0 if rc == 0 else 1
    if args.command == "setup":
        _setup(backend, args)
    else:
        if args.phase is None:
            parser.error("phase needs setup, validate or agent")
        if backend.sandbox_profile is None:
            parser.error("phase needs --profile-json")
        backend._set_egress_phase(args.phase)
    print(f"DRIVER_OK {args.command} {args.phase or ''}".rstrip(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
