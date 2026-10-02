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

    def test_string_exit_code_zero_not_failure(self, capsys):
        """Codex may serialize exit_code as the string "0"; treat it as success."""
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item(
                "command_execution",
                command="true",
                exit_code="0",
                status="completed",
            ),
        )
        proc.process_line(line)
        assert "exit=" not in capsys.readouterr().out

    def test_string_exit_code_nonzero_is_failure(self, capsys):
        """A string exit_code like "2" is still rendered as a failure."""
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item(
                "command_execution",
                command="false",
                exit_code="2",
                status="failed",
            ),
        )
        proc.process_line(line)
        assert "exit=2" in capsys.readouterr().out

    def test_thinking_uses_cyan_color(self, capsys):
        """Reasoning output uses cyan (not red, which is reserved for errors)."""
        proc = CodexStreamProcessor(color=True)
        line = _make_event(
            "item.completed",
            item=_item("reasoning", text="Checking the repository"),
        )
        proc.process_line(line)
        assert "\033[3;36m" in capsys.readouterr().out

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

    def test_failed_mcp_tool_call(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item(
                "mcp_tool_call",
                server="github",
                tool="get_pr",
                status="failed",
                error={"message": "repository unavailable"},
            ),
        )
        proc.process_line(line)
        output = capsys.readouterr().out
        assert "❌ MCP github/get_pr failed: repository unavailable" in output

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

    def test_wraps_long_agent_message(self, capsys):
        proc = CodexStreamProcessor(color=False, wrap=30)
        line = _make_event(
            "item.completed",
            item=_item(
                "agent_message",
                text="one two three four five six seven eight nine ten",
            ),
        )
        proc.process_line(line)
        output = capsys.readouterr().out
        assert "💬 Codex one two three four five" in output
        assert "\n    six seven eight nine ten\n" in output


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


# Shapes below mirror a real Codex 0.153.4 ``exec --json`` stream (autofix job
# 16893128852): a model-metadata warning arrives as an ``error`` item, and
# spawn_agent calls that fail emit ``item.started`` with no ``item.completed``.
_METADATA_WARNING = (
    "Model metadata for `gpt-6-sol` not found. Defaulting to fallback metadata; "
    "this can degrade performance and cause issues."
)


def _turn_completed(**usage):
    return _make_event("turn.completed", usage=usage)


def _spawn(item_id, status="in_progress", **kwargs):
    return _item(
        "collab_tool_call",
        item_id=item_id,
        tool="spawn_agent",
        sender_thread_id="thread-parent",
        receiver_thread_ids=[],
        prompt="You are the IMPLEMENT agent.",
        agents_states=kwargs.pop("agents_states", {}),
        status=status,
        **kwargs,
    )


def _nested_command(command, output, item_id="item_9"):
    return _make_event(
        "item.completed",
        item=_item(
            "command_execution",
            item_id=item_id,
            command=command,
            aggregated_output=output,
            exit_code=0,
            status="completed",
        ),
    )


_NESTED_HUMAN_OUTPUT = (
    "OpenAI Codex v0.153.4\n--------\nworkdir: /sandbox/repo\nmodel: gpt-6-sol\n"
    "--------\nuser\nImplement the fix.\n"
    f"warning: {_METADATA_WARNING}\n"
    "codex\nUpdated the constraint.\n"
    "tokens used\n86,066\n"
    "Updated the constraint.\n"
)


class TestErrorItems:
    def test_error_item_prints_warning(self, capsys):
        proc = CodexStreamProcessor(color=False)
        line = _make_event(
            "item.completed",
            item=_item("error", item_id="item_0", message=_METADATA_WARNING),
        )
        assert proc.process_line(line) is False
        output = capsys.readouterr().out
        assert f"⚠️ Codex warning: {_METADATA_WARNING}" in output

    def test_error_item_uses_warning_color(self, capsys):
        proc = CodexStreamProcessor(color=True)
        line = _make_event("item.completed", item=_item("error", message="careful"))
        proc.process_line(line)
        assert "\033[33m⚠️ Codex warning: careful" in capsys.readouterr().out

    def test_duplicate_warnings_collapse(self, capsys):
        proc = CodexStreamProcessor(color=False)
        for index in range(4):
            proc.process_line(
                _make_event(
                    "item.completed",
                    item=_item("error", item_id=f"item_{index}", message=_METADATA_WARNING),
                )
            )
        assert proc.process_line(_turn_completed()) is True
        output = capsys.readouterr().out
        assert output.count("Codex warning:") == 1
        assert "3 repeated or additional Codex warning/failure line(s) suppressed" in output

    def test_distinct_warnings_are_capped(self, capsys):
        proc = CodexStreamProcessor(color=False)
        total = CodexStreamProcessor._MAX_NOTICES + 5
        for index in range(total):
            proc.process_line(
                _make_event("item.completed", item=_item("error", message=f"warning {index}"))
            )
        proc.flush_errors()
        output = capsys.readouterr().out
        assert output.count("Codex warning:") == CodexStreamProcessor._MAX_NOTICES
        assert "5 repeated or additional" in output

    def test_warning_is_single_line_and_capped(self, capsys):
        proc = CodexStreamProcessor(color=False)
        message = "first line\nsecond line " + "x" * 500
        proc.process_line(_make_event("item.completed", item=_item("error", message=message)))
        output = capsys.readouterr().out
        lines = [line for line in output.splitlines() if "Codex warning:" in line]
        assert len(lines) == 1
        assert "first line second line" in lines[0]
        assert lines[0].endswith("…")
        shown = lines[0].split("Codex warning: ", 1)[1]
        assert len(shown) == CodexStreamProcessor._MAX_NOTICE_CHARS

    def test_summary_printed_once(self, capsys):
        proc = CodexStreamProcessor(color=False)
        for _ in range(2):
            proc.process_line(_make_event("item.completed", item=_item("error", message="dup")))
        proc.process_line(_turn_completed())
        proc.flush_errors()
        assert capsys.readouterr().out.count("suppressed") == 1


class TestCollabToolCalls:
    def test_started_collab_call_is_shown(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_make_event("item.started", item=_spawn("item_13")))
        output = capsys.readouterr().out
        assert "🤖 Agent spawn_agent" in output
        assert "IMPLEMENT" not in output

    def test_unfinished_spawns_reported_at_turn_end(self, capsys):
        proc = CodexStreamProcessor(color=False)
        for item_id in ("item_13", "item_15", "item_17"):
            proc.process_line(_make_event("item.started", item=_spawn(item_id)))
        assert proc.process_line(_turn_completed(input_tokens=10, output_tokens=2)) is True
        output = capsys.readouterr().out
        assert (
            "⚠️ Codex spawn_agent: 3 calls never completed (failed or interrupted; "
            "the stream carries no result)" in output
        )
        assert "likely failed" not in output
        assert output.index("never completed") < output.index("TOKENS")

    def test_polling_collab_calls_have_no_start_line(self, capsys):
        proc = CodexStreamProcessor(color=False)
        for index in range(3):
            item = _item("collab_tool_call", item_id=f"item_{index}", tool="wait")
            proc.process_line(_make_event("item.started", item=item))
        assert capsys.readouterr().out == ""

    def test_collab_tool_name_is_single_line_and_capped(self, capsys):
        proc = CodexStreamProcessor(color=False)
        tool = "bad\ntool " + "z" * 200
        item = _item("collab_tool_call", tool=tool, status="failed")
        proc.process_line(_make_event("item.completed", item=item))
        output = capsys.readouterr().out
        lines = [line for line in output.splitlines() if "failed:" in line]
        assert len(lines) == 1
        shown = lines[0].split("❌ Codex ", 1)[1].split(" failed:", 1)[0]
        assert shown.startswith("bad tool zzz")
        assert len(shown) == CodexStreamProcessor._MAX_TOOL_CHARS
        assert shown.endswith("…")

    def test_unfinished_spawns_reported_on_failed_turn(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_make_event("item.started", item=_spawn("item_13")))
        proc.process_line(_make_event("turn.failed", error={"message": "boom"}))
        proc.flush_errors()
        output = capsys.readouterr().out
        assert "Codex spawn_agent: 1 call never completed" in output
        assert "boom" in output

    def test_failed_collab_call_with_agent_error(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_make_event("item.started", item=_spawn("item_13")))
        completed = _spawn(
            "item_13",
            status="failed",
            agents_states={
                "thread-child": {
                    "status": "errored",
                    "message": "reasoning effort high\nis not supported " + "y" * 400,
                }
            },
        )
        proc.process_line(_make_event("item.completed", item=completed))
        proc.process_line(_turn_completed())
        output = capsys.readouterr().out
        lines = [line for line in output.splitlines() if "failed:" in line]
        assert len(lines) == 1
        assert "❌ Codex spawn_agent failed: reasoning effort high is not supported" in lines[0]
        assert lines[0].endswith("…")
        assert "never completed" not in output

    def test_failed_collab_call_without_detail(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_make_event("item.completed", item=_spawn("item_13", status="failed")))
        output = capsys.readouterr().out
        assert "❌ Codex spawn_agent failed: no error detail in stream" in output

    def test_errored_agent_state_on_completed_wait(self, capsys):
        proc = CodexStreamProcessor(color=False)
        item = _item(
            "collab_tool_call",
            tool="wait",
            status="completed",
            agents_states={"thread-child": {"status": "errored", "message": "crashed"}},
        )
        proc.process_line(_make_event("item.completed", item=item))
        assert "❌ Codex wait failed: crashed" in capsys.readouterr().out

    def test_duplicate_collab_failures_collapse(self, capsys):
        proc = CodexStreamProcessor(color=False)
        for item_id in ("item_13", "item_15"):
            proc.process_line(_make_event("item.completed", item=_spawn(item_id, status="failed")))
        proc.flush_errors()
        output = capsys.readouterr().out
        assert output.count("spawn_agent failed") == 1
        assert "1 repeated or additional" in output

    def test_successful_collab_call_is_quiet(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_make_event("item.started", item=_spawn("item_13")))
        done = _spawn(
            "item_13",
            status="completed",
            agents_states={"thread-child": {"status": "running", "message": None}},
        )
        proc.process_line(_make_event("item.completed", item=done))
        proc.process_line(_turn_completed())
        output = capsys.readouterr().out
        assert "failed" not in output
        assert "never completed" not in output
        assert "suppressed" not in output


class TestNestedTokens:
    _COMMAND = "/bin/bash -lc \"timeout 900 codex -C /sandbox/repo exec --ephemeral 'fix it'\""

    def test_nested_human_runs_added_to_totals(self, capsys):
        proc = CodexStreamProcessor(color=False)
        review_output = _NESTED_HUMAN_OUTPUT.replace("86,066", "35,113")
        proc.process_line(_nested_command(self._COMMAND, _NESTED_HUMAN_OUTPUT, "item_20"))
        proc.process_line(_nested_command(self._COMMAND, review_output, "item_21"))
        line = _turn_completed(
            input_tokens=1622378,
            cached_input_tokens=1559623,
            cache_write_input_tokens=60171,
            output_tokens=6584,
        )
        assert proc.process_line(line) is True
        output = capsys.readouterr().out
        assert "TOKENS in=1622378 out=6584 cache_r=1559623 cache_w=60171 total=1628962" in output
        assert (
            "TOKENS nested_runs=2 nested_used=121179 parent_used=69339 combined_used=190518"
            in output
        )

    def test_nested_json_run_added_to_totals(self, capsys):
        proc = CodexStreamProcessor(color=False)
        nested = "\n".join(
            [
                _make_event("thread.started", thread_id="child"),
                _turn_completed(input_tokens=1000, cached_input_tokens=400, output_tokens=50),
            ]
        )
        proc.process_line(_nested_command("codex exec --json 'review'", nested))
        proc.process_line(_turn_completed(input_tokens=100, output_tokens=10))
        output = capsys.readouterr().out
        assert "nested_runs=1 nested_used=650 parent_used=110 combined_used=760" in output

    def test_non_codex_command_ignored(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_nested_command("cat old-run.log", _NESTED_HUMAN_OUTPUT))
        proc.process_line(_turn_completed(input_tokens=1, output_tokens=1))
        assert "nested" not in capsys.readouterr().out

    def test_codex_command_without_run_header_ignored(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_nested_command("codex --help", "usage\ntokens used\n500\n"))
        proc.process_line(_turn_completed(input_tokens=1, output_tokens=1))
        assert "nested" not in capsys.readouterr().out

    def test_no_nested_line_without_nested_runs(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_turn_completed(input_tokens=1, output_tokens=1))
        output = capsys.readouterr().out
        assert "TOKENS in=1" in output
        assert "nested" not in output

    def test_malformed_tokens_used_does_not_crash(self, capsys):
        proc = CodexStreamProcessor(color=False)
        output = "OpenAI Codex v1\ntokens used\n,\n"
        assert proc.process_line(_nested_command("codex exec 'x'", output)) is False
        proc.process_line(_turn_completed(input_tokens=1, output_tokens=1))
        assert "nested" not in capsys.readouterr().out

    def test_logs_printed_by_the_nested_run_are_not_counted(self, capsys):
        proc = CodexStreamProcessor(color=False)
        # The nested agent cat'ed an older run's log; only the run's own final
        # "tokens used" counts.
        old_log = _NESTED_HUMAN_OUTPUT.replace("86,066", "999,999")
        transcript = _NESTED_HUMAN_OUTPUT.replace(
            "codex\nUpdated the constraint.\n",
            f"exec\ncat /tmp/codex-old.log\n{old_log}codex\nUpdated the constraint.\n",
        )
        assert transcript.count("tokens used") == 2
        proc.process_line(_nested_command(self._COMMAND, transcript))
        proc.process_line(_turn_completed(input_tokens=100, output_tokens=10))
        assert "nested_runs=1 nested_used=86066 " in capsys.readouterr().out

    def test_rereading_a_saved_log_is_not_counted(self, capsys):
        proc = CodexStreamProcessor(color=False)
        proc.process_line(_nested_command(self._COMMAND, _NESTED_HUMAN_OUTPUT, "item_20"))
        proc.process_line(_nested_command("cat /tmp/codex-impl.log", _NESTED_HUMAN_OUTPUT))
        proc.process_line(_turn_completed(input_tokens=100, output_tokens=10))
        assert "nested_runs=1 nested_used=86066 " in capsys.readouterr().out

    def test_two_invocations_in_one_command(self, capsys):
        proc = CodexStreamProcessor(color=False)
        review_output = _NESTED_HUMAN_OUTPUT.replace("86,066", "35,113")
        command = "codex exec 'implement'; codex exec 'review'"
        proc.process_line(_nested_command(command, _NESTED_HUMAN_OUTPUT + review_output))
        proc.process_line(_turn_completed(input_tokens=100, output_tokens=10))
        assert "nested_runs=2 nested_used=121179 " in capsys.readouterr().out

    def test_json_turns_counted_once_per_thread(self, capsys):
        proc = CodexStreamProcessor(color=False)
        turn = _turn_completed(input_tokens=1000, cached_input_tokens=400, output_tokens=50)
        nested = "\n".join([turn, _make_event("thread.started", thread_id="child"), turn, turn])
        proc.process_line(_nested_command("codex exec --json 'review'", nested))
        proc.process_line(_turn_completed(input_tokens=100, output_tokens=10))
        assert "nested_runs=1 nested_used=650 " in capsys.readouterr().out

    def test_json_events_without_json_flag_ignored(self, capsys):
        proc = CodexStreamProcessor(color=False)
        nested = "\n".join(
            [
                _make_event("thread.started", thread_id="child"),
                _turn_completed(input_tokens=1000, output_tokens=50),
            ]
        )
        proc.process_line(_nested_command("codex exec 'review'", nested))
        proc.process_line(_turn_completed(input_tokens=1, output_tokens=1))
        assert "nested" not in capsys.readouterr().out
