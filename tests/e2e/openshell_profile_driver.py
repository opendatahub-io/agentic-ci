"""E2E driver for sandbox-profile egress on the OpenShell backend.

``tests/e2e/e2e-openshell-profile.sh`` probes the network from inside the
sandbox; this driver does the parts that are Python API only. It builds an
:class:`~agentic_ci.backends.openshell.OpenShellBackend` for the Codex
harness, optionally with a central sandbox profile, and then either creates
the sandbox (``setup``) or switches the setup shim's egress (``phase``).

No agent runs, so no LLM call is made. The OpenAI provider still needs a
key to be created; the shell script passes a fake one.

Usage::

    python3 tests/e2e/openshell_profile_driver.py setup --image IMG --workdir DIR \\
        [--profile-json '{"egress": ["npm", "goproxy"]}']
    python3 tests/e2e/openshell_profile_driver.py phase {setup,validate,agent} \\
        --image IMG --workdir DIR --profile-json '{"egress": ["npm", "goproxy"]}'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentic_ci.backends.openshell import OpenShellBackend
from agentic_ci.harness import CodexHarness
from agentic_ci.sandbox_profile import parse_profile


def _backend(args: argparse.Namespace) -> OpenShellBackend:
    profile = None
    if args.profile_json:
        profile = parse_profile(json.loads(args.profile_json), source="central").profile
    return OpenShellBackend(
        workdir=str(args.workdir),
        image=args.image,
        harness=CodexHarness(),
        sandbox_profile=profile,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E driver for sandbox-profile egress.")
    parser.add_argument("command", choices=["setup", "phase"])
    parser.add_argument("phase", nargs="?", choices=["setup", "validate", "agent"])
    parser.add_argument("--image", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--profile-json", default=None)
    args = parser.parse_args()

    backend = _backend(args)
    if args.command == "setup":
        backend.setup()
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
