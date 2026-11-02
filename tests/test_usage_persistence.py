"""Usage persistence through the CLI and skill-session lifecycle."""

import argparse
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from agentic_ci import cli, otel, skill
from agentic_ci.harness import create_harness
from tests.test_otel import _codex_completed_log, _named_metric_points, _read_log, _write_jsonl


@pytest.mark.parametrize("entrypoint", ["cli", "skill"])
@pytest.mark.parametrize("append_error", [None, OSError("disk full"), ValueError("invalid totals")])
def test_usage_saved_after_flush_and_before_cleanup(
    tmp_path, monkeypatch, capsys, caplog, entrypoint, append_error
):
    events = []
    log_path = tmp_path / "claude-otel.jsonl"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    monkeypatch.setenv("GITHUB_WORKSPACE", str(artifact_dir))
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("CODEX_SUBAGENT_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("OTEL_RATE_FILE", "")
    backend = mock.Mock(collector_bind_address="127.0.0.1")
    backend.run.return_value = 0
    backend.stop.side_effect = lambda: events.append("cleanup")
    harness = create_harness("codex")
    owner = cli.otel if entrypoint == "cli" else skill
    records = [
        _codex_completed_log("gpt-6.1-sol", 100, 20),
        _codex_completed_log("gpt-6-luna", 50, 10),
    ]

    def flush(_proc):
        events.append("flush")
        _write_jsonl(log_path, records)

    original_append = otel.append_codex_usage

    def append(path, summary, **kwargs):
        events.append("append")
        assert kwargs["start_ns"] > 0
        if append_error:
            raise append_error
        return original_append(path, summary, **kwargs)

    monkeypatch.setattr(
        owner,
        "start_collector",
        lambda *args, **kwargs: (SimpleNamespace(pid=123), 4318, log_path, str(tmp_path / "rate")),
    )
    monkeypatch.setattr(owner, "stop_collector", flush)
    monkeypatch.setattr(owner, "inject_root_spans", lambda *args, **kwargs: 0)
    monkeypatch.setattr(owner, "append_codex_usage", append)

    if entrypoint == "cli":
        original_print = otel.print_summary

        def print_summary(path):
            events.append("summary")
            return original_print(path)

        monkeypatch.setattr(otel, "print_summary", print_summary)
        args = argparse.Namespace(
            prompt="pong",
            backend="local",
            workdir=str(tmp_path),
            model=None,
            effort=None,
            no_otel=False,
            no_streaming=True,
            keep=False,
            extra_args=[],
            pre_gates=None,
            post_gates=None,
        )
        with pytest.raises(SystemExit) as exc:
            cli.cmd_run(args, backend, harness)
        assert exc.value.code == 0
        assert events == ["flush", "summary", "append", "cleanup"]
        # This copy is made by cmd_run before backend.stop(). It must include
        # the appended metrics, rather than only the original completion logs.
        saved = _read_log(artifact_dir / log_path.name)
    else:
        monkeypatch.setattr(skill, "create_backend", lambda *args, **kwargs: backend)
        monkeypatch.setattr(skill, "create_harness", lambda *args, **kwargs: harness)
        with skill._AgentSession(tmp_path, harness_name="codex") as session:
            session.run("classify", model="gpt-6.1-sol")
            session.run("skill", model="gpt-6-luna")
        assert events == ["flush", "append", "cleanup"]
        saved = _read_log(log_path)

    points = _named_metric_points(saved, "agentic_ci.codex.cost.usage")
    if append_error:
        assert not points
        assert "not saved (non-fatal)" in capsys.readouterr().err + caplog.text
    else:
        assert len(points) == 2
        assert otel.parse_metrics(saved)[:2] == otel.parse_metrics(records)[:2]


@pytest.mark.parametrize("cost", [0.0, 1.0])
def test_metric_types_and_cumulative_timestamps(tmp_path, cost):
    path = tmp_path / "otel.jsonl"
    summary = otel.UsageSummary(
        frozenset({"gpt-6.1-sol"}), {("gpt-6.1-sol", "input"): 10}, {"gpt-6.1-sol": cost}
    )
    assert otel.append_codex_usage(path, summary, start_ns=123)
    metrics = _read_log(path)[0]["payload"]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    for metric in metrics:
        assert metric["sum"]["aggregationTemporality"] == 2
        assert metric["sum"]["isMonotonic"] is True
        for point in metric["sum"]["dataPoints"]:
            assert point["startTimeUnixNano"] == "123"
            assert int(point["timeUnixNano"]) >= 123
            if metric["name"].endswith("cost.usage"):
                assert point["asDouble"] == cost
                assert "asInt" not in point
            else:
                assert point["asInt"] == 10


def test_native_codex_metrics_are_not_estimated_totals(tmp_path):
    path = tmp_path / "otel.jsonl"
    path.write_text(
        json.dumps(
            {
                "path": "/v1/metrics",
                "payload": {
                    "resourceMetrics": [
                        {
                            "scopeMetrics": [
                                {
                                    "scope": {"name": "codex"},
                                    "metrics": [
                                        {
                                            "name": "codex.cost.usage",
                                            "sum": {
                                                "dataPoints": [
                                                    {
                                                        "asDouble": 99,
                                                        "attributes": [
                                                            {
                                                                "key": "model",
                                                                "value": {
                                                                    "stringValue": "gpt-6.1-sol"
                                                                },
                                                            }
                                                        ],
                                                    }
                                                ]
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    summary = otel.read_usage_summary(path)
    assert summary == otel.UsageSummary(frozenset(), {}, {})
    assert not otel.append_codex_usage(path, summary)


def test_claude_session_preserves_native_metrics(tmp_path, monkeypatch):
    """Codex persistence must not introduce parsing failures into Claude runs."""
    path = tmp_path / "claude-otel.jsonl"
    record = {
        "path": "/v1/metrics",
        "payload": {
            "resourceMetrics": [
                {
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {"dataPoints": [{"asInt": "10"}]},
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    }
    _write_jsonl(path, [record])
    backend = mock.Mock(collector_bind_address="127.0.0.1")
    monkeypatch.setattr(skill, "create_backend", lambda *args, **kwargs: backend)
    monkeypatch.setattr(
        skill, "start_collector", lambda *args, **kwargs: (object(), 4318, path, None)
    )
    monkeypatch.setattr(skill, "stop_collector", lambda *args: None)
    monkeypatch.setattr(skill, "inject_root_spans", lambda *args, **kwargs: 0)
    with skill._AgentSession(tmp_path, harness_name="claude-code"):
        pass
    backend.stop.assert_called_once()
    assert _read_log(path) == [record]
