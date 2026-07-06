"""Tests for ClaudeCodeStreamProcessor."""

import json

from agentic_ci.stream import ClaudeCodeStreamProcessor


def _make_result(is_error=False, **kwargs):
    msg = {
        "type": "result",
        "subtype": "success",
        "stop_reason": "stop_sequence",
        "duration_ms": 400,
        "duration_api_ms": 0,
        "ttft_ms": 0,
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "result": "",
        **kwargs,
    }
    if is_error:
        msg["is_error"] = True
    return json.dumps(msg)


class TestProcessLineResult:
    def test_successful_result_completes_stream(self):
        proc = ClaudeCodeStreamProcessor(color=False)
        assert proc.process_line(_make_result()) is True

    def test_error_result_does_not_complete_stream(self):
        proc = ClaudeCodeStreamProcessor(color=False)
        line = _make_result(
            is_error=True,
            api_error_status=403,
            result="Permission denied",
        )
        assert proc.process_line(line) is False

    def test_error_result_is_error_false_completes_stream(self):
        proc = ClaudeCodeStreamProcessor(color=False)
        line = _make_result(is_error=False)
        assert proc.process_line(line) is True


class TestFormatResultError:
    def test_error_result_prints_error_label(self, capsys):
        proc = ClaudeCodeStreamProcessor(color=False)
        proc.process_line(_make_result(is_error=True, result="Permission denied"))
        out = capsys.readouterr().out
        assert "ERROR:" in out
        assert "Permission denied" in out

    def test_successful_result_no_error_label(self, capsys):
        proc = ClaudeCodeStreamProcessor(color=False)
        proc.process_line(_make_result())
        out = capsys.readouterr().out
        assert "ERROR:" not in out
