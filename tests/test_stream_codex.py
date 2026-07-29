"""Tests for CodexStreamProcessor."""

import json

from agentic_ci.stream import CodexStreamProcessor


def _make_event(event_type, **kwargs):
    event = {"type": event_type}
    event.update(kwargs)
    return json.dumps(event)


def _item(item_type, item_id="item_1", **kwargs):
    item = {"id": item_id, "type": item_type}
    item.update(kwargs)
    return item


class TestProcessLine:
    def test_empty_line(self):
        assert CodexStreamProcessor(color=False).process_line("") is False

    def test_invalid_json(self):
        assert CodexStreamProcessor(color=False).process_line("not json") is False

    def test_agent_message(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item("agent_message", text="Done with analysis"),
        )
        assert proc.process_line(line) is False
        assert "💬 Codex Done with analysis" in capsys.readouterr().out

    def test_reasoning(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item("reasoning", text="Checking the repository"),
        )
        proc.process_line(line)
        assert "Thinking Checking the repository" in capsys.readouterr().out

    def test_command_execution(self, capsys):
        proc = CodexStreamProcessor(color=False)
        started = _make_event(
            "item.started",
            item=_item(
                "command_execution",
                command="/bin/bash -lc 'ls -la'",
                status="in_progress",
            ),
        )
        completed = _make_event(
            "item.completed",
            item=_item(
                "command_execution",
                command="/bin/bash -lc 'ls -la'",
                exit_code=0,
                status="completed",
            ),
        )
        assert proc.process_line(started) is False
        assert proc.process_line(completed) is False
        output = capsys.readouterr().out
        assert "Shell $" in output
        assert "ls -la" in output
        assert output.count("Shell $") == 1

    def test_failed_command_prints_exit_code(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item(
                "command_execution",
                command="false",
                exit_code=1,
                status="failed",
            ),
        )
        proc.process_line(line)
        output = capsys.readouterr().out
        assert "Shell $ false" in output
        assert "exit=1" in output

    def test_file_change(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item(
                "file_change",
                changes=[{"path": "src/app.py", "kind": "update"}],
            ),
        )
        proc.process_line(line)
        assert "File change update src/app.py" in capsys.readouterr().out

    def test_mcp_tool_call(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.started",
            item=_item("mcp_tool_call", server="github", tool="get_pr"),
        )
        proc.process_line(line)
        assert "MCP github/get_pr" in capsys.readouterr().out

    def test_web_search(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.started",
            item=_item("web_search", query="Codex telemetry"),
        )
        proc.process_line(line)
        assert "Web search Codex telemetry" in capsys.readouterr().out

    def test_turn_completed(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "turn.completed",
            usage={
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 10,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
        )
        assert proc.process_line(line) is True
        output = capsys.readouterr().out
        assert "TOKENS in=100 out=20 cache_r=40 cache_w=10 total=120" in output

    def test_error_event(self, capsys):
        proc = CodexStreamProcessor(color=False)
        assert proc.process_line(_make_event("error", message="token invalid")) is False
        proc.flush_errors()
        assert "token invalid" in capsys.readouterr().out

    def test_turn_failed(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("turn.failed", error={"message": "request failed"})
        assert proc.process_line(line) is False
        proc.flush_errors()
        assert "request failed" in capsys.readouterr().out

    def test_unknown_type_ignored(self):
        proc = CodexStreamProcessor(color=False)
        assert proc.process_line(_make_event("something_unknown", data="foo")) is False


class TestProcess:
    def test_full_run(self):
        proc = CodexStreamProcessor(color=False)
        events = [
            _make_event("thread.started", thread_id="thread-1"),
            _make_event("turn.started"),
            _make_event(
                "item.completed",
                item=_item("agent_message", text="Finished"),
            ),
            _make_event("turn.completed", usage={}),
        ]
        assert proc.process(events) is True

    def test_incomplete_stream(self):
        proc = CodexStreamProcessor(color=False)
        events = [_make_event("turn.started")]
        assert proc.process(events) is False

    def test_bytes_input(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item("agent_message", text="Hello"),
        )
        assert proc.process([line.encode("utf-8")]) is False
        assert "Hello" in capsys.readouterr().out
