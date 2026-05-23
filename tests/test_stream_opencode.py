"""Tests for OpenCodeStreamProcessor."""

import json

from agentic_ci.stream import OpenCodeStreamProcessor


def _make_event(event_type, **kwargs):
    """Build an OpenCode JSONL event."""
    event = {"type": event_type, "timestamp": 1234567890, "sessionID": "test-session"}
    event.update(kwargs)
    return json.dumps(event)


class TestProcessLine:
    def test_empty_line(self):
        proc = OpenCodeStreamProcessor(color=False)
        assert proc.process_line("") is False

    def test_invalid_json(self):
        proc = OpenCodeStreamProcessor(color=False)
        assert proc.process_line("not json") is False

    def test_step_start(self):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event("step_start", part={"type": "step-start"})
        assert proc.process_line(line) is False

    def test_text_event(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "text",
            part={"type": "text", "text": "Hello world"},
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Agent" in captured.out
        assert "Hello world" in captured.out

    def test_tool_use_event(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "tool_use",
            part={
                "type": "tool",
                "tool": "bash",
                "callID": "call-1",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls -la", "description": "list files"},
                    "output": "file1\nfile2\n",
                },
                "title": "list files",
            },
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Bash" in captured.out
        assert "ls -la" in captured.out

    def test_step_finish_stop(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "step_finish",
            part={
                "type": "step-finish",
                "reason": "stop",
                "tokens": {
                    "total": 1000,
                    "input": 500,
                    "output": 100,
                    "reasoning": 0,
                    "cache": {"write": 400, "read": 0},
                },
                "cost": 0.0123,
            },
        )
        assert proc.process_line(line) is True
        captured = capsys.readouterr()
        assert "TOKENS" in captured.out
        assert "in=500" in captured.out
        assert "out=100" in captured.out
        assert "cost=$0.0123" in captured.out

    def test_step_finish_tool_calls(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "step_finish",
            part={
                "type": "step-finish",
                "reason": "tool-calls",
                "tokens": {
                    "total": 500,
                    "input": 300,
                    "output": 50,
                    "reasoning": 0,
                    "cache": {"write": 0, "read": 150},
                },
                "cost": 0.005,
            },
        )
        assert proc.process_line(line) is False

    def test_error_event(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "error",
            error={"name": "UnknownError", "data": {"message": "Model not found"}},
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Model not found" in captured.out


class TestProcess:
    def test_full_run(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        events = [
            _make_event("step_start", part={"type": "step-start"}),
            _make_event("text", part={"type": "text", "text": "Hello"}),
            _make_event(
                "step_finish",
                part={
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {
                        "total": 100,
                        "input": 50,
                        "output": 10,
                        "reasoning": 0,
                        "cache": {"write": 40, "read": 0},
                    },
                    "cost": 0.001,
                },
            ),
        ]
        result = proc.process(events)
        assert result is True

    def test_incomplete_stream(self):
        proc = OpenCodeStreamProcessor(color=False)
        events = [
            _make_event("step_start", part={"type": "step-start"}),
            _make_event("text", part={"type": "text", "text": "Hello"}),
        ]
        result = proc.process(events)
        assert result is False

    def test_bytes_input(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event("text", part={"type": "text", "text": "Hello"})
        result = proc.process([line.encode("utf-8")])
        assert result is False
        captured = capsys.readouterr()
        assert "Hello" in captured.out


class TestToolFormatting:
    def test_read_tool(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "tool_use",
            part={
                "type": "tool",
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"file_path": "/tmp/test.py"},
                },
            },
        )
        proc.process_line(line)
        captured = capsys.readouterr()
        assert "/tmp/test.py" in captured.out

    def test_edit_tool(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "tool_use",
            part={
                "type": "tool",
                "tool": "edit",
                "state": {
                    "status": "completed",
                    "input": {
                        "file_path": "/tmp/test.py",
                        "old_string": "foo",
                        "new_string": "bar",
                    },
                },
            },
        )
        proc.process_line(line)
        captured = capsys.readouterr()
        assert "/tmp/test.py" in captured.out
