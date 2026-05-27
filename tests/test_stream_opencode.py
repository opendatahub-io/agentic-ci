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
        proc.flush_errors()
        captured = capsys.readouterr()
        assert "Model not found" in captured.out

    def test_error_dedup_generic(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        specific = _make_event(
            "error",
            error={"name": "UnknownError", "data": {"message": "Model not found: foo"}},
        )
        generic = _make_event(
            "error",
            error={
                "name": "UnknownError",
                "data": {"message": "Unexpected server error. Check server logs for details."},
            },
        )
        proc.process_line(specific)
        proc.process_line(generic)
        proc.flush_errors()
        captured = capsys.readouterr()
        assert "Model not found: foo" in captured.out
        assert "Unexpected server error" not in captured.out

    def test_error_generic_only(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        generic = _make_event(
            "error",
            error={
                "name": "UnknownError",
                "data": {"message": "Unexpected server error. Check server logs for details."},
            },
        )
        proc.process_line(generic)
        proc.flush_errors()
        captured = capsys.readouterr()
        assert "Common causes:" in captured.out
        assert "invalid model name" in captured.out


class TestThinking:
    def test_thinking_event(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "thinking",
            part={"type": "thinking", "text": "Let me analyze this code"},
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "\U0001f9e0" in captured.out
        assert "Thinking" in captured.out
        assert "Let me analyze this code" in captured.out

    def test_thinking_ends_on_text(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        proc.process_line(_make_event("thinking", part={"type": "thinking", "text": "Hmm"}))
        proc.process_line(_make_event("text", part={"type": "text", "text": "Hello"}))
        captured = capsys.readouterr()
        assert "Thinking" in captured.out
        assert "Agent" in captured.out

    def test_thinking_ends_on_tool(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        proc.process_line(_make_event("thinking", part={"type": "thinking", "text": "Planning"}))
        proc.process_line(
            _make_event(
                "tool_use",
                part={
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "input": {"command": "ls"}},
                },
            )
        )
        captured = capsys.readouterr()
        assert "Thinking" in captured.out
        assert "Bash" in captured.out


class TestFallbackTruncation:
    def test_long_value_truncated(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        long_val = "x" * 100
        line = _make_event(
            "tool_use",
            part={
                "type": "tool",
                "tool": "custom_tool",
                "state": {"status": "completed", "input": {"data": long_val}},
            },
        )
        proc.process_line(line)
        captured = capsys.readouterr()
        assert "x" * 60 + "…" in captured.out
        assert "x" * 100 not in captured.out


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

    def test_read_tool_camelcase(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "tool_use",
            part={
                "type": "tool",
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "/workspace/README.md"},
                },
                "title": "README.md",
            },
        )
        proc.process_line(line)
        captured = capsys.readouterr()
        assert "/workspace/README.md" in captured.out

    def test_edit_tool_camelcase(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "tool_use",
            part={
                "type": "tool",
                "tool": "edit",
                "state": {
                    "status": "completed",
                    "input": {
                        "filePath": "/tmp/test.py",
                        "oldString": "foo",
                        "newString": "bar",
                    },
                },
            },
        )
        proc.process_line(line)
        captured = capsys.readouterr()
        assert "/tmp/test.py" in captured.out

    def test_task_tool(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "tool_use",
            part={
                "type": "tool",
                "tool": "task",
                "state": {
                    "status": "completed",
                    "input": {
                        "description": "Explore codebase structure",
                        "subagent_type": "explore",
                        "prompt": "Thoroughly explore the codebase\nLine 2\nLine 3",
                    },
                    "output": (
                        "task_id: ses_abc123\n\n<task_result>\n"
                        "Found 5 files.\nAll tests pass.\n</task_result>"
                    ),
                },
                "title": "Explore codebase structure",
            },
        )
        proc.process_line(line)
        captured = capsys.readouterr()
        assert "\U0001f916" in captured.out
        assert "[explore]" in captured.out
        assert "Explore codebase structure" in captured.out
        assert "Thoroughly explore the codebase" in captured.out
        assert "Line 2" in captured.out
        assert "Found 5 files." in captured.out
        assert "All tests pass." in captured.out
        assert "task_id" not in captured.out
        assert "task_result" not in captured.out

    def test_task_tool_camelcase(self, capsys):
        proc = OpenCodeStreamProcessor(color=False)
        line = _make_event(
            "tool_use",
            part={
                "type": "tool",
                "tool": "task",
                "state": {
                    "status": "completed",
                    "input": {
                        "description": "Verify README",
                        "subagentType": "explore",
                        "prompt": "Check the README",
                    },
                },
            },
        )
        proc.process_line(line)
        captured = capsys.readouterr()
        assert "[explore]" in captured.out
        assert "Verify README" in captured.out
