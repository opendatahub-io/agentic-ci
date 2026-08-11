"""Tests for CursorStreamProcessor."""

import json
from pathlib import Path

from agentic_ci.stream import CursorStreamProcessor, _extract_cursor_tool

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "cursor"


def _make_event(event_type, **kwargs):
    """Build a Cursor NDJSON event."""
    event = {"type": event_type, "session_id": "test-session"}
    event.update(kwargs)
    return json.dumps(event)


class TestProcessLine:
    def test_empty_line(self):
        proc = CursorStreamProcessor(color=False)
        assert proc.process_line("") is False

    def test_invalid_json(self):
        proc = CursorStreamProcessor(color=False)
        assert proc.process_line("not json at all") is False

    def test_malformed_json(self):
        proc = CursorStreamProcessor(color=False)
        assert proc.process_line('{"type": "system", broken') is False

    def test_unknown_event_type(self):
        proc = CursorStreamProcessor(color=False)
        line = _make_event("future_event", data={"foo": "bar"})
        assert proc.process_line(line) is False


class TestSystemEvent:
    def test_init_event(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "system",
            subtype="init",
            model="Sonnet 4.6 1M Thinking",
            permissionMode="default",
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Cursor Agent" in captured.out
        assert "Sonnet 4.6 1M Thinking" in captured.out
        assert "default" in captured.out

    def test_system_unknown_subtype(self):
        proc = CursorStreamProcessor(color=False)
        line = _make_event("system", subtype="unknown_subtype")
        assert proc.process_line(line) is False


class TestUserEvent:
    def test_user_event_skipped(self):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "user",
            message={"role": "user", "content": [{"type": "text", "text": "hello"}]},
        )
        assert proc.process_line(line) is False


class TestThinkingEvent:
    def test_thinking_delta(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event("thinking", subtype="delta", text="Let me analyze")
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "\U0001f9e0" in captured.out
        assert "Thinking" in captured.out

    def test_thinking_completed(self, capsys):
        proc = CursorStreamProcessor(color=False)
        proc.process_line(_make_event("thinking", subtype="delta", text="Hmm"))
        proc.process_line(_make_event("thinking", subtype="completed"))
        assert proc._in_thinking is False

    def test_thinking_multiple_deltas(self, capsys):
        proc = CursorStreamProcessor(color=False)
        proc.process_line(_make_event("thinking", subtype="delta", text="Let"))
        proc.process_line(_make_event("thinking", subtype="delta", text=" me"))
        proc.process_line(_make_event("thinking", subtype="delta", text=" think"))
        proc.process_line(_make_event("thinking", subtype="completed"))
        captured = capsys.readouterr()
        assert "Thinking" in captured.out


class TestAssistantEvent:
    def test_assistant_text(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "assistant",
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello world"}],
            },
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Agent" in captured.out
        assert "Hello world" in captured.out

    def test_assistant_ends_thinking(self, capsys):
        proc = CursorStreamProcessor(color=False)
        proc.process_line(_make_event("thinking", subtype="delta", text="Planning"))
        proc.process_line(
            _make_event(
                "assistant",
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done"}],
                },
            )
        )
        captured = capsys.readouterr()
        assert "Thinking" in captured.out
        assert "Agent" in captured.out


class TestToolCallEvent:
    def test_shell_tool_started(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "tool_call",
            subtype="started",
            call_id="call-1",
            tool_call={
                "shellToolCall": {
                    "args": {"command": "ls -la"},
                    "description": "list files",
                },
            },
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "\U0001f527" in captured.out
        assert "Bash" in captured.out
        assert "ls -la" in captured.out

    def test_read_tool_started(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "tool_call",
            subtype="started",
            call_id="call-2",
            tool_call={
                "readToolCall": {
                    "args": {"path": "/tmp/test.py"},
                },
            },
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Read" in captured.out
        assert "/tmp/test.py" in captured.out

    def test_tool_call_completed_ignored(self):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "tool_call",
            subtype="completed",
            call_id="call-1",
            tool_call={
                "shellToolCall": {
                    "args": {"command": "ls"},
                    "result": {"success": {"exitCode": 0}},
                },
            },
        )
        assert proc.process_line(line) is False

    def test_tool_call_ends_thinking(self, capsys):
        proc = CursorStreamProcessor(color=False)
        proc.process_line(_make_event("thinking", subtype="delta", text="Planning"))
        proc.process_line(
            _make_event(
                "tool_call",
                subtype="started",
                call_id="call-1",
                tool_call={"shellToolCall": {"args": {"command": "ls"}}},
            )
        )
        captured = capsys.readouterr()
        assert "Thinking" in captured.out
        assert "Bash" in captured.out

    def test_task_tool_started(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "tool_call",
            subtype="started",
            call_id="call-task",
            tool_call={
                "taskToolCall": {
                    "args": {"description": "investigate parity gaps"},
                },
            },
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Task" in captured.out
        assert "investigate parity gaps" in captured.out

    def test_fetch_tool_started(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "tool_call",
            subtype="started",
            call_id="call-fetch",
            tool_call={
                "fetchToolCall": {
                    "args": {"url": "https://example.com"},
                },
            },
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Fetch" in captured.out
        assert "https://example.com" in captured.out


class TestResultEvent:
    def test_success_result(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "result",
            subtype="success",
            duration_ms=14779,
            duration_api_ms=14779,
            is_error=False,
            result="hello world",
            usage={
                "inputTokens": 4,
                "outputTokens": 104,
                "cacheReadTokens": 27563,
                "cacheWriteTokens": 27715,
            },
        )
        assert proc.process_line(line) is True
        captured = capsys.readouterr()
        assert "Result: success" in captured.out
        assert "14.8s" in captured.out
        assert "in=4" in captured.out
        assert "out=104" in captured.out
        assert "cache_r=27563" in captured.out

    def test_error_result(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "result",
            subtype="error",
            duration_ms=1000,
            duration_api_ms=1000,
            is_error=True,
            result="Something went wrong",
            usage={"inputTokens": 0, "outputTokens": 0},
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "ERROR: error" in captured.out
        assert "Something went wrong" in captured.out

    def test_result_no_usage(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "result",
            subtype="success",
            duration_ms=5000,
            duration_api_ms=5000,
            is_error=False,
            result="ok",
        )
        assert proc.process_line(line) is True


class TestExtractCursorTool:
    def test_shell_tool(self):
        name, params = _extract_cursor_tool(
            {"shellToolCall": {"args": {"command": "ls"}, "description": "list"}}
        )
        assert name == "Bash"
        assert params["command"] == "ls"
        assert params["description"] == "list"

    def test_read_tool(self):
        name, params = _extract_cursor_tool({"readToolCall": {"args": {"path": "/tmp/test.py"}}})
        assert name == "Read"
        assert params["file_path"] == "/tmp/test.py"

    def test_write_tool(self):
        name, params = _extract_cursor_tool({"writeToolCall": {"args": {"path": "/tmp/out.txt"}}})
        assert name == "Write"
        assert params["file_path"] == "/tmp/out.txt"

    def test_edit_tool(self):
        name, params = _extract_cursor_tool(
            {
                "editToolCall": {
                    "args": {
                        "path": "/tmp/test.py",
                        "old_string": "foo",
                    }
                }
            }
        )
        assert name == "Edit"
        assert params["file_path"] == "/tmp/test.py"
        assert params["old_string"] == "foo"

    def test_grep_tool(self):
        name, params = _extract_cursor_tool(
            {"grepToolCall": {"args": {"pattern": "TODO", "path": "src/"}}}
        )
        assert name == "Grep"
        assert params["pattern"] == "TODO"
        assert params["path"] == "src/"

    def test_glob_tool(self):
        name, params = _extract_cursor_tool(
            {"globToolCall": {"args": {"glob_pattern": "*.py", "path": "."}}}
        )
        assert name == "Glob"
        assert params["pattern"] == "*.py"

    def test_task_tool(self):
        name, params = _extract_cursor_tool(
            {"taskToolCall": {"args": {"description": "review diff"}}}
        )
        assert name == "Task"
        assert params["description"] == "review diff"

    def test_fetch_tool(self):
        name, params = _extract_cursor_tool(
            {"fetchToolCall": {"args": {"url": "https://example.com"}}}
        )
        assert name == "Fetch"
        assert params["url"] == "https://example.com"

    def test_unknown_tool(self):
        name, params = _extract_cursor_tool({"weirdTool": {"data": 1}})
        assert name == "Unknown"


class TestProcess:
    def test_full_run(self, capsys):
        proc = CursorStreamProcessor(color=False)
        events = [
            _make_event("system", subtype="init", model="test-model", permissionMode="default"),
            _make_event(
                "user",
                message={"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ),
            _make_event("thinking", subtype="delta", text="Let me think"),
            _make_event("thinking", subtype="completed"),
            _make_event(
                "tool_call",
                subtype="started",
                call_id="c1",
                tool_call={"shellToolCall": {"args": {"command": "echo hi"}}},
            ),
            _make_event(
                "tool_call",
                subtype="completed",
                call_id="c1",
                tool_call={"shellToolCall": {"args": {"command": "echo hi"}}},
            ),
            _make_event(
                "assistant",
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done!"}],
                },
            ),
            _make_event(
                "result",
                subtype="success",
                duration_ms=5000,
                duration_api_ms=5000,
                is_error=False,
                result="Done!",
                usage={"inputTokens": 10, "outputTokens": 20},
            ),
        ]
        result = proc.process(events)
        assert result is True
        captured = capsys.readouterr()
        assert "Cursor Agent" in captured.out
        assert "Thinking" in captured.out
        assert "Bash" in captured.out
        assert "Agent" in captured.out
        assert "Result: success" in captured.out

    def test_incomplete_stream(self):
        proc = CursorStreamProcessor(color=False)
        events = [
            _make_event("system", subtype="init", model="test-model"),
            _make_event("thinking", subtype="delta", text="Hmm"),
        ]
        result = proc.process(events)
        assert result is False

    def test_bytes_input(self, capsys):
        proc = CursorStreamProcessor(color=False)
        line = _make_event(
            "assistant",
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
            },
        )
        result = proc.process([line.encode("utf-8")])
        assert result is False
        captured = capsys.readouterr()
        assert "Hello" in captured.out


class TestFixtures:
    """Test against real captured Cursor CLI output."""

    def test_simple_echo_fixture(self, capsys):
        fixture = FIXTURES_DIR / "simple-echo.jsonl"
        if not fixture.exists():
            return
        proc = CursorStreamProcessor(color=False)
        with open(fixture) as f:
            result = proc.process(f)
        assert result is True
        captured = capsys.readouterr()
        assert "Cursor Agent" in captured.out
        assert "Bash" in captured.out
        assert "echo hello world" in captured.out
        assert "Result: success" in captured.out

    def test_file_read_fixture(self, capsys):
        fixture = FIXTURES_DIR / "file-read.jsonl"
        if not fixture.exists():
            return
        proc = CursorStreamProcessor(color=False)
        with open(fixture) as f:
            result = proc.process(f)
        assert result is True
        captured = capsys.readouterr()
        assert "Cursor Agent" in captured.out
        assert "Read" in captured.out
        assert "Result: success" in captured.out
