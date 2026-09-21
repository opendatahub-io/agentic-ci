"""E2E driver for ``run_routed_skill()``.

The e2e shell scripts under ``tests/e2e/`` exercise the ``agentic-ci`` CLI,
but model routing is a Python API. This driver runs one routed skill with a
trivial prompt against a real backend and harness, then verifies the
artifacts routing is expected to leave behind:

- ``verdict.json`` written by the main run and loaded by the skill engine
- ``_run/route.json`` written by the classifier (unless fallback expected)
- ``_run/classifier-output.txt`` and ``agent-output.txt`` stream tees
- a ``skill.routed`` span in ``_run/claude-otel.jsonl`` parented under the
  run root, and a synthetic root span whose ``agent.model`` is the routed
  model (harnesses with OTEL support only)

It prints one ``ROUTED_RESULT`` line and one ``ROUTED_CHECK`` line per
check, then exits non-zero when the run failed or any check failed. Shell
scripts grep those lines.

Usage::

    python3 tests/e2e/routed_skill_driver.py --backend local --harness claude-code \\
        --workdir /tmp/work [--image IMG] [--force-tier high] [--classifier-noop] \\
        [--expect-source classifier|forced|fallback] [--model-tier low=MODEL:EFFORT ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentic_ci.routing import ModelTier, route_path
from agentic_ci.skill import SkillConfig, run_routed_skill

SKILL_PROMPT = (
    "Write the file verdict.json in the current directory with exactly this content: "
    '{"verdict": "committed"}. Then reply with only the word pong.'
)
NOOP_CLASSIFIER_PROMPT = "Reply with only the word pong. Do not read or write any files."


def _otel_spans(run_dir: Path) -> list[dict]:
    log_file = run_dir / "claude-otel.jsonl"
    if not log_file.exists():
        return []
    spans: list[dict] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("path") != "/v1/traces":
            continue
        for rs in record.get("payload", {}).get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                spans.extend(ss.get("spans", []))
    return spans


def _attr(span: dict, key: str):
    for attr in span.get("attributes", []):
        if attr.get("key") == key:
            return attr.get("value", {}).get("stringValue")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E driver for run_routed_skill().")
    parser.add_argument("--backend", required=True, choices=["local", "podman", "openshell"])
    parser.add_argument("--harness", required=True, choices=["claude-code", "opencode", "codex"])
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--image", default=None)
    parser.add_argument("--force-tier", default=None)
    parser.add_argument(
        "--classifier-noop",
        action="store_true",
        help="Use a classifier prompt that never writes route.json (forces fallback).",
    )
    parser.add_argument("--classifier-max-turns", type=int, default=10)
    parser.add_argument("--expect-source", default="classifier")
    parser.add_argument(
        "--model-tier",
        action="append",
        default=[],
        metavar="TIER=MODEL[:EFFORT]",
        help="Override a tier, e.g. low=claude-sonnet-4-5:medium (repeatable).",
    )
    args = parser.parse_args()

    model_tiers: dict[str, ModelTier] = {}
    for spec in args.model_tier:
        tier, _, model_spec = spec.partition("=")
        model, _, effort = model_spec.partition(":")
        model_tiers[tier] = ModelTier(model, effort or None)

    work_dir: Path = args.workdir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    config = SkillConfig(
        skill_name="e2e-routed",
        prompt_builder=lambda **kw: SKILL_PROMPT,
        verdict_loader=lambda wd: json.loads((wd / "verdict.json").read_text(encoding="utf-8")),
        backend_name=args.backend,
        harness_name=args.harness,
        container_image=args.image,
        model_tiers=model_tiers,
    )
    result = run_routed_skill(
        config,
        ticket_key="E2E-1",
        work_dir=work_dir,
        config_dir=work_dir,
        classifier_max_turns=args.classifier_max_turns,
        classifier_prompt_builder=(lambda _task: NOOP_CLASSIFIER_PROMPT)
        if args.classifier_noop
        else None,
        force_tier=args.force_tier,
    )
    route = result.route
    print(
        "ROUTED_RESULT "
        f"rc={result.rc} "
        f"source={route.source if route else None} "
        f"tier={route.tier if route else None} "
        f"model={route.model if route else None} "
        f"effort={route.effort if route else None}",
        flush=True,
    )

    checks: dict[str, bool] = {}
    run_dir = work_dir / "_run"
    checks["rc_zero"] = result.rc == 0
    checks["route_present"] = route is not None
    checks["source_expected"] = route is not None and route.source == args.expect_source
    checks["verdict_file"] = (work_dir / "verdict.json").is_file()
    checks["agent_output"] = (work_dir / "agent-output.txt").is_file()
    if args.expect_source == "classifier":
        checks["route_file"] = route_path(work_dir).is_file()
        checks["classifier_output"] = (run_dir / "classifier-output.txt").is_file()
    elif args.expect_source == "fallback":
        checks["route_file_absent"] = not route_path(work_dir).exists()
    elif args.expect_source == "forced":
        checks["classifier_output_absent"] = not (run_dir / "classifier-output.txt").exists()

    spans = _otel_spans(run_dir)
    routed = [s for s in spans if s.get("name") == "skill.routed"]
    roots = [s for s in spans if not s.get("parentSpanId")]
    root_ids = {s.get("spanId") for s in roots}
    checks["routed_event"] = len(routed) == 1
    checks["routed_event_parented"] = len(routed) == 1 and routed[0].get("parentSpanId") in root_ids
    if routed:
        payload = json.loads(routed[0]["events"][0]["attributes"][0]["value"]["stringValue"])
        checks["routed_event_matches"] = route is not None and (
            payload.get("model") == route.model and payload.get("source") == route.source
        )
    model_roots = [s for s in roots if _attr(s, "agent.model") is not None]
    checks["root_span_present"] = len(model_roots) >= 1
    checks["root_span_model"] = route is not None and any(
        _attr(s, "agent.model") == route.model for s in model_roots
    )

    for name, ok in checks.items():
        print(f"ROUTED_CHECK {name}={'ok' if ok else 'fail'}", flush=True)

    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
