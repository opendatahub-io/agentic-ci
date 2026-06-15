---
name: debug-agentic-ci
description: Investigate infrastructure-level failures in the agentic-ci framework. Covers container backend issues, skill engine errors, forge MR/PR failures, and gate problems. Use when the container failed, MR operations broke, or the skill engine returned errors.
allowed-tools: Bash Read Grep Glob
---

# Debug Agentic-CI

Investigate failures in the CI infrastructure layer: container backends, skill engine, forge operations, gates, and telemetry.

## Key entry points

| Area | Entry point |
|------|-------------|
| CLI + orchestration | `src/agentic_ci/cli.py` |
| Skill engine | `src/agentic_ci/skill.py` (`run_skill()`, `SkillConfig`) |
| Podman backend | `src/agentic_ci/backends/podman.py` |
| OpenShell backend | `src/agentic_ci/backends/openshell/` |
| Forge (MR/PR) | `src/agentic_ci/forge.py` |
| Git operations | `src/agentic_ci/git.py` |
| Gates | `src/agentic_ci/gates.py` |
| Jira client | `src/agentic_ci/jira.py` |
| Stream parsing | `src/agentic_ci/stream.py` |
| OTEL telemetry | `src/agentic_ci/otel.py` |
| Container images | `images/` |

## Protocol

1. **Read** this repo's `AGENTS.md` for architecture context.
2. **Check** [references/symptoms.md](references/symptoms.md) for known patterns matching the failure.
3. **Investigate** the relevant module following the entry points above.
4. **Write RCA** using [assets/rca-template.md](assets/rca-template.md).
