"""Tests for the default skill container runner."""

from unittest import mock

from agentic_ci import skill
from agentic_ci.harness import CodexHarness


class _Backend:
    collector_bind_address = "127.0.0.1"

    def setup(self, otel_port=None):
        pass

    def run(self, prompt, model, otel_port=None, traceparent=None):
        return 0

    def stop(self):
        pass


def test_default_runner_logs_codex_reasoning_efforts_before_setup(monkeypatch, tmp_path):
    backend = _Backend()
    events = []
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("CODEX_SUBAGENT_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(skill, "create_harness", lambda name: CodexHarness())
    monkeypatch.setattr(skill, "create_backend", lambda *args, **kwargs: backend)
    monkeypatch.setattr(
        skill,
        "start_collector",
        mock.Mock(side_effect=RuntimeError("collector unavailable")),
    )
    monkeypatch.setattr(skill.log, "info", lambda message, value: events.append((message, value)))
    backend.setup = lambda otel_port=None: events.append(("setup", "called"))

    rc = skill._default_run_container(
        tmp_path,
        "prompt",
        tmp_path / "output.log",
        harness_name="codex",
        backend_name="local",
    )

    assert rc == 0
    assert events == [
        ("Reasoning effort: %s", "high"),
        ("Sub-agent reasoning effort: %s", "high"),
        ("setup", "called"),
    ]


def test_default_runner_logs_codex_reasoning_effort_overrides(monkeypatch, tmp_path):
    backend = _Backend()
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "medium")
    monkeypatch.setenv("CODEX_SUBAGENT_REASONING_EFFORT", "xhigh")
    monkeypatch.setattr(skill, "create_harness", lambda name: CodexHarness())
    monkeypatch.setattr(skill, "create_backend", lambda *args, **kwargs: backend)
    monkeypatch.setattr(
        skill,
        "start_collector",
        mock.Mock(side_effect=RuntimeError("collector unavailable")),
    )

    with mock.patch.object(skill.log, "info") as info:
        rc = skill._default_run_container(
            tmp_path,
            "prompt",
            tmp_path / "output.log",
            harness_name="codex",
            backend_name="local",
        )

    assert rc == 0
    info.assert_has_calls(
        [
            mock.call("Reasoning effort: %s", "medium"),
            mock.call("Sub-agent reasoning effort: %s", "xhigh"),
        ]
    )
