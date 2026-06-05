# API Reference

Auto-generated reference documentation for all public modules in `agentic-ci`.

## Core

| Module | Description |
|--------|-------------|
| [CLI](cli.md) | Entry point, backend/harness selection, OTEL orchestration |
| [Backend](backend.md) | Abstract base class for sandbox backends |
| [Harness](harness.md) | Harness abstraction for AI agent CLI tools |
| [Stream Processors](stream.md) | Parsers for Claude Code and OpenCode output |
| [OTEL Collector](otel.md) | OTLP HTTP/JSON receiver and token/cost summary |

## Backends

| Module | Description |
|--------|-------------|
| [Factory & Podman](backends.md) | Backend registry, factory, and Podman container backend |
| [OpenShell](openshell.md) | OpenShell sandbox backend, gateway, and policy |

## Pipeline

| Module | Description |
|--------|-------------|
| [Gates](gates.md) | Pre- and post-agent validation gates |
| [Skill Runner](skill.md) | Generic skill runner framework |
| [Verdict](verdict.md) | Structured verdict JSON validation |
| [Git Operations](git.md) | Git operations for CI pipelines |
| [Pipeline Generation](pipeline.md) | GitLab child pipeline YAML generation |

## Integrations

| Module | Description |
|--------|-------------|
| [Forge](forge.md) | Git forge abstraction for GitHub and GitLab |
| [Jira](jira.md) | Jira REST client, ADF conversion, acli wrapper |

## Utilities

| Module | Description |
|--------|-------------|
| [Logging](log.md) | Colored CLI output helpers |
| [Skill Metadata](skill_metadata.md) | YAML frontmatter parser for SKILL.md files |
