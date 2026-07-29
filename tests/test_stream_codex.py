"""Tests for CodexStreamProcessor."""

import json

from agentic_ci.stream import CodexStreamProcessor


def _make_event(event_type, **kwargs):
    """Build a Codex JSONL event."""
    event = {"type": event_type}
    event.update(kwargs)
    return json.dumps(event)


class TestProcessLine:
    def test_empty_line(self):
        proc = CodexStreamProcessor(color=False)
        assert proc.process_line("") is False

    def test_invalid_json(self):
        proc = CodexStreamProcessor(color=False)
        assert proc.process_line("not json") is False

    def test_message_delta(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("message_delta", delta="Hello world\n")
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Codex" in captured.out
        assert "Hello world" in captured.out

    def test_message_complete(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("message_complete", content="Done with analysis")
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Codex" in captured.out
        assert "Done with analysis" in captured.out

    def test_exec_approval_shell(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "exec_approval",
            command={"type": "shell", "command": ["ls", "-la"]},
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "Shell" in captured.out
        assert "ls -la" in captured.out

    def test_exec_approval_file_edit(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "exec_approval",
            command={"type": "file_edit", "path": "/tmp/test.py"},
        )
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "FileEdit" in captured.out
        assert "/tmp/test.py" in captured.out

    def test_exec_result_success(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("exec_result", exit_code=0)
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        # exit_code=0 should not print anything
        assert "exit=" not in captured.out

    def test_exec_result_failure(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("exec_result", exit_code=1)
        assert proc.process_line(line) is False
        captured = capsys.readouterr()
        assert "exit=1" in captured.out

    def test_task_complete(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("task_complete", summary="All done")
        assert proc.process_line(line) is True
        captured = capsys.readouterr()
        assert "Task complete" in captured.out
        assert "All done" in captured.out

    def test_error_event(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("error", message="API key invalid")
        assert proc.process_line(line) is False
        proc.flush_errors()
        captured = capsys.readouterr()
        assert "API key invalid" in captured.out

    def test_unknown_type_ignored(self):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("something_unknown", data="foo")
        assert proc.process_line(line) is False


class TestProcess:
    def test_full_run(self, capsys):
        proc = CodexStreamProcessor(color=False)
        events = [
            _make_event("message_delta", delta="Working on it..."),
            _make_event(
                "exec_approval",
                command={"type": "shell", "command": ["echo", "hello"]},
            ),
            _make_event("exec_result", exit_code=0),
            _make_event("task_complete", summary="Finished"),
        ]
        result = proc.process(events)
        assert result is True

    def test_incomplete_stream(self):
        proc = CodexStreamProcessor(color=False)
        events = [
            _make_event("message_delta", delta="Hello"),
        ]
        result = proc.process(events)
        assert result is False

    def test_bytes_input(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event("message_complete", content="Hello")
        result = proc.process([line.encode("utf-8")])
        assert result is False
        captured = capsys.readouterr()
        assert "Hello" in captured.out
