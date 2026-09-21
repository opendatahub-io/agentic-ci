"""Tests for reasoning effort handling in the ``agentic-ci run`` command."""

import argparse

import pytest

from agentic_ci.cli import cmd_run
from agentic_ci.harness import create_harness


class RecordingBackend:
    collector_bind_address = "127.0.0.1"

    def __init__(self):
        self.run_kwargs = None

    def setup(self, otel_port=None):
        pass

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return 0

    def stop(self):
        pass


def _run(args, backend, harness):
    """cmd_run ends with sys.exit(rc); return the exit code."""
    with pytest.raises(SystemExit) as exc:
        cmd_run(args, backend, harness)
    return exc.value.code


def _args(tmp_path, **overrides):
    values = {
        "prompt": "pong",
        "backend": "local",
        "workdir": str(tmp_path),
        "model": None,
        "effort": None,
        "no_otel": True,
        "no_streaming": True,
        "keep": True,
        "extra_args": ["--max-turns", "3"],
        "pre_gates": None,
        "post_gates": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_default_effort_is_high(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_REASONING_EFFORT", raising=False)
    backend = RecordingBackend()
    assert _run(_args(tmp_path), backend, create_harness("claude-code")) == 0
    assert backend.run_kwargs["extra_args"] == ["--effort", "high", "--max-turns", "3"]


def test_flag_overrides_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SUBAGENT_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "medium")
    backend = RecordingBackend()
    assert _run(_args(tmp_path, effort="xhigh"), backend, create_harness("codex")) == 0
    assert backend.run_kwargs["extra_args"][:4] == [
        "-c",
        "model_reasoning_effort=xhigh",
        "-c",
        "agents.default_subagent_reasoning_effort=xhigh",
    ]


def test_invalid_effort_fails_before_agent_starts(tmp_path, capsys):
    backend = RecordingBackend()
    assert _run(_args(tmp_path, effort="bogus"), backend, create_harness("opencode")) == 1
    assert "Unsupported OpenCode effort 'bogus'" in capsys.readouterr().err
    assert backend.run_kwargs is None


def test_none_passes_no_effort_flag(tmp_path, capsys):
    backend = RecordingBackend()
    assert _run(_args(tmp_path, effort="none"), backend, create_harness("claude-code")) == 0
    assert backend.run_kwargs["extra_args"] == ["--max-turns", "3"]
    assert "Reasoning effort: none" in capsys.readouterr().out
