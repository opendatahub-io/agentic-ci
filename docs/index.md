# agentic-ci

Run AI coding agents in sandboxed CI environments with streaming output
and telemetry. Supports multiple agent harnesses (Claude Code, OpenCode)
and isolation backends so you can choose the right tradeoff between
simplicity and security.

## Features

- **Multiple backends**: Local (direct execution), Podman containers, or OpenShell sandboxes with network policy enforcement
- **Multiple harnesses**: Claude Code and OpenCode agent CLIs
- **Streaming output**: Colored, parsed CI logs with tool call summaries and token tracking
- **OTEL telemetry**: Token usage, cost tracking, and metrics collection
- **Gates**: Pre- and post-agent validation (sensitive files, commit checks, secret scanning)
- **Skill runner**: Generic framework for building AI-powered CI pipelines

## Install

```bash
uv tool install agentic-ci
# OR
pip install agentic-ci
```

## Quick start

```bash
# Run locally (no container needed)
agentic-ci run --backend local "Fix the flaky test in test_auth.py"

# Run with Podman (default)
agentic-ci run "Fix the flaky test in test_auth.py" \
    --image ghcr.io/opendatahub-io/ai-helpers:latest

# Run with OpenShell sandbox
agentic-ci run --backend openshell "Fix the flaky test in test_auth.py"
```

## Python API

```python
from agentic_ci.backends import create_backend
from agentic_ci.harness import create_harness

harness = create_harness("claude-code")
backend = create_backend(
    "podman",
    harness=harness,
    workdir="/path/to/repo",
    image="my-image:latest",
)
backend.setup()
rc = backend.run(prompt="Fix the bug", model="claude-sonnet-4-6")
backend.stop()
```

## Learn more

- [API Reference](api/index.md) -- auto-generated from source docstrings
- [GitHub Repository](https://github.com/opendatahub-io/agentic-ci)
- [PyPI Package](https://pypi.org/project/agentic-ci/)
