"""Stream processors for AI agent output formats."""

import json
import os
import re
import signal
import sys
import time

from agentic_ci import log


def _get(params, *keys):
    """Get the first non-empty value for any of the given keys."""
    for k in keys:
        v = params.get(k, "")
        if v:
            return v
    return ""


def _format_tool(name, params):
    """Return a compact one-line summary for known tools."""
    name = name[0].upper() + name[1:] if name else name
    if name == "Bash":
        cmd = params.get("command", "")
        desc = params.get("description", "")
        return f"$ {cmd}" + (f"  # {desc}" if desc else "")
    if name == "Read":
        path = _get(params, "file_path", "filePath")
        parts = [path]
        if "offset" in params:
            parts.append(f"L{params['offset']}")
        if "limit" in params:
            parts.append(f"+{params['limit']}")
        return " ".join(parts)
    if name == "Write":
        return _get(params, "file_path", "filePath")
    if name == "Edit":
        path = _get(params, "file_path", "filePath")
        old = _get(params, "old_string", "oldString")
        preview = old.split("\n")[0][:60]
        if len(old) > len(preview):
            preview += "…"
        return f"{path}: {preview}"
    if name == "Glob":
        pattern = params.get("pattern", "")
        path = params.get("path", ".")
        return f"{pattern} in {path}"
    if name == "Grep":
        pattern = params.get("pattern", "")
        path = params.get("path", ".")
        return f"/{pattern}/ in {path}"
    if name in ("Agent", "Task"):
        desc = params.get("description", "")
        agent_type = _get(params, "subagent_type", "subagentType")
        return f"[{agent_type}] {desc}" if agent_type else desc
    if name == "Skill":
        skill = params.get("skill", "")
        skill_args = params.get("args", "")
        return f"/{skill} {skill_args}".strip()
    if name in ("TaskGet", "Taskget"):
        return _get(params, "task_id", "taskId")
    parts = []
    for k, v in params.items():
        val = str(v)
        if len(val) > 60:
            val = val[:60] + "…"
        parts.append(f"{k}={val}")
    result = ", ".join(parts)
    if len(result) > 200:
        result = result[:200] + "…"
    return result


class ClaudeCodeStreamProcessor:
    """Processes Claude Code stream-json and prints human-readable output."""

    def __init__(self, color=True, wrap=0, claude_pid=0):
        self.wrap = wrap
        self.claude_pid = claude_pid

        if color:
            self.THINK = "\033[3;36m"
            self.TOOL = "\033[1;90m"
            self.CLAUDE = ""
            self.RED = "\033[31m"
            self.YELLOW = "\033[33m"
            self.RESET = "\033[0m"
        else:
            self.THINK = self.TOOL = self.CLAUDE = ""
            self.RED = self.YELLOW = self.RESET = ""

        self._in_text = False
        self._in_thinking = False
        self._last_block_type = None
        self._tool_name = None
        self._tool_json = ""
        self._line_buf = ""
        self._total_input = 0
        self._total_output = 0
        self._total_cache_read = 0
        self._total_cache_write = 0
        self._last_emitted_total = 0
        self._last_emitted_time = 0.0

    def _emit(self, text):
        self._line_buf += text
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            if self.wrap and len(line) > self.wrap:
                wrapped = ""
                col = 0
                for word in line.split(" "):
                    if col + len(word) > self.wrap and col > 0:
                        wrapped += "\n"
                        col = 0
                    if col > 0:
                        wrapped += " "
                        col += 1
                    wrapped += word
                    col += len(word)
                print(wrapped, flush=True)
            else:
                print(line, flush=True)

    def _flush_emit(self):
        if self._line_buf:
            print(self._line_buf, flush=True)
            self._line_buf = ""

    def _end_block(self):
        if self._in_text:
            self._flush_emit()
            sys.stdout.write(self.RESET + "\n")
            self._in_text = False
        if self._in_thinking:
            self._flush_emit()
            sys.stdout.write(self.RESET + "\n")
            self._in_thinking = False
        if self._tool_name:
            try:
                parsed = json.loads(self._tool_json)
            except (json.JSONDecodeError, ValueError):
                parsed = None

            self._last_block_type = "tool"
            summary = _format_tool(self._tool_name, parsed) if parsed else None

            if summary:
                icon = "\U0001f916" if self._tool_name == "Agent" else "\U0001f527"
                print(f"  {self.TOOL}{icon} {self._tool_name} {summary}{self.RESET}", flush=True)
            else:
                print(f"  {self.TOOL}\U0001f527 {self._tool_name}{self.RESET}", flush=True)
                if parsed:
                    formatted = json.dumps(parsed, indent=2)
                    for line in formatted.split("\n"):
                        print(f"    {self.TOOL}{line}{self.RESET}", flush=True)
            self._tool_name = None
            self._tool_json = ""

    def _format_init(self, msg):
        version = msg.get("claude_code_version", "unknown")
        model = msg.get("model", "unknown")
        perms = msg.get("permissionMode", "unknown")
        tools = msg.get("tools", [])
        mcp = msg.get("mcp_servers", [])
        agents = msg.get("agents", [])
        plugins = msg.get("plugins", [])
        log.section(f"Claude Code v{version}")
        log.detail("Model", model)
        log.detail("Permissions", perms)
        log.detail("Tools", f"{len(tools)} available")
        if mcp:
            log.detail(
                "MCP servers",
                ", ".join(s.get("name", s) if isinstance(s, dict) else s for s in mcp),
            )
        else:
            log.detail("MCP servers", "none")
        log.detail("Agents", f"{len(agents)} available")
        if plugins:
            log.detail(
                "Plugins",
                ", ".join(p.get("name", str(p)) if isinstance(p, dict) else p for p in plugins),
            )

    def _format_result(self, msg):
        subtype = msg.get("subtype", "unknown")
        stop = msg.get("stop_reason", "")
        is_error = msg.get("is_error", False)
        duration_ms = msg.get("duration_ms", 0)
        api_ms = msg.get("duration_api_ms", 0)
        ttft_ms = msg.get("ttft_ms", 0)
        turns = msg.get("num_turns", 0)
        cost = msg.get("total_cost_usd", 0)
        label = f"{subtype} ({stop})" if stop else subtype
        if is_error:
            label = f"ERROR: {label}"
        log.section(f"Result: {label}")
        log.detail(
            "Duration",
            f"{duration_ms / 1000:.1f}s (API: {api_ms / 1000:.1f}s, TTFT: {ttft_ms / 1000:.1f}s)",
        )
        log.detail("Turns", str(turns))
        log.detail("Cost", f"${cost:.4f}")
        if is_error:
            error_text = msg.get("result", "")
            if error_text:
                log.detail("Error", error_text)

    def flush_errors(self):
        # Claude Code reports errors inline; nothing to flush.
        pass

    def process_line(self, line):
        """Process a single line of stream-json. Returns True if run is complete."""
        line = line.strip()
        if not line:
            return False

        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False

        msg_type = msg.get("type")

        if msg_type == "system":
            subtype = msg.get("subtype", "")
            if subtype == "init":
                self._format_init(msg)
            elif subtype == "api_retry":
                attempt = msg.get("attempt", "?")
                max_retries = msg.get("max_retries", "?")
                delay = msg.get("retry_delay_ms", "?")
                error = msg.get("error", "unknown")
                self._last_block_type = "system"
                print(
                    f"  {self.YELLOW}\U0001f504 Retry {attempt}/{max_retries}{self.RESET} "
                    f"{error} — retrying in {delay}ms",
                    flush=True,
                )
            return False

        if msg_type == "user":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, str) and content.strip() == "FULL RUN COMPLETE":
                        self._end_block()
                        if self.claude_pid:
                            log.section("FULL RUN COMPLETE detected, terminating Claude")
                            try:
                                os.kill(self.claude_pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        return True
            return False

        if msg_type == "result":
            self._end_block()
            self._format_result(msg)
            if msg.get("is_error"):
                return False
            return True

        if msg_type != "stream_event":
            return False

        event = msg.get("event", {})
        event_type = event.get("type")

        if event_type == "content_block_start":
            block = event.get("content_block", {})
            block_type = block.get("type")
            if block_type == "text":
                self._last_block_type = "text"
                print(f"  {self.CLAUDE}\U0001f4ac Claude ", end="", flush=True)
                self._in_text = True
            elif block_type == "thinking":
                self._last_block_type = "thinking"
                print(f"  {self.THINK}\U0001f9e0 Thinking ", end="", flush=True)
                self._in_thinking = True
            elif block_type in ("tool_use", "server_tool_use"):
                self._tool_name = block.get("name", "unknown")
                self._tool_json = ""

        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                self._emit(delta.get("text", ""))
            elif delta_type == "thinking_delta":
                self._emit(delta.get("thinking", ""))
            elif delta_type == "input_json_delta":
                self._tool_json += delta.get("partial_json", "")

        elif event_type == "content_block_stop":
            self._end_block()

        elif event_type == "message_start":
            usage = event.get("message", {}).get("usage", {})
            self._total_input = usage.get("input_tokens", 0)
            self._total_cache_read = usage.get("cache_read_input_tokens", 0)
            self._total_cache_write = usage.get("cache_creation_input_tokens", 0)

        elif event_type == "message_delta":
            usage = event.get("usage", {})
            out = usage.get("output_tokens", 0)
            if out > 0:
                self._total_output = out
                total = (
                    self._total_input
                    + self._total_output
                    + self._total_cache_read
                    + self._total_cache_write
                )
                if total - self._last_emitted_total >= 5_000 or self._last_emitted_total == 0:
                    now = time.monotonic()
                    rate = 0.0
                    try:
                        with open(
                            os.environ.get("OTEL_RATE_FILE", "/tmp/claude-otel-rate.json")
                        ) as rf:
                            rd = json.load(rf)
                        rate = rd.get("rate", 0)
                    except Exception:
                        pass
                    if rate <= 0 and self._last_emitted_time > 0:
                        dt = now - self._last_emitted_time
                        dv = total - self._last_emitted_total
                        if dt > 0:
                            rate = dv / dt
                    rate_str = f" rate={rate:.0f}/s" if rate > 0 else ""
                    self._last_emitted_total = total
                    self._last_emitted_time = now
                    print(
                        f"{self.TOOL}  \U0001f4ca TOKENS in={self._total_input} "
                        f"out={self._total_output} "
                        f"cache_r={self._total_cache_read} "
                        f"cache_w={self._total_cache_write} "
                        f"total={total}{rate_str}{self.RESET}",
                        flush=True,
                    )

        elif event_type == "error":
            error = event.get("error", {})
            error_type = error.get("type", "unknown")
            error_msg = error.get("message", "")
            self._last_block_type = "error"
            print(
                f"  {self.RED}❌ Error: {error_type}: {error_msg}{self.RESET}",
                flush=True,
            )

        return False

    def process(self, input_stream):
        """Process a stream of lines. Returns True if run completed normally."""
        for line in input_stream:
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            if self.process_line(line):
                return True
        return False


class OpenCodeStreamProcessor:
    """Processes OpenCode JSON output and prints human-readable CI logs."""

    def __init__(self, color=True, wrap=0, agent_pid=0):
        self.wrap = wrap
        self.agent_pid = agent_pid

        if color:
            self.THINK = "\033[3;36m"
            self.TOOL = "\033[1;90m"
            self.AGENT = ""
            self.RED = "\033[31m"
            self.RESET = "\033[0m"
        else:
            self.THINK = self.TOOL = self.AGENT = self.RED = self.RESET = ""

        self._in_text = False
        self._in_thinking = False
        self._emitted_first_line = False
        self._line_buf = ""
        self._errors: list[str] = []

    _INDENT = "  "

    def _print_line(self, line):
        prefix = self._INDENT if self._in_text and self._emitted_first_line else ""
        if self.wrap and len(line) > self.wrap:
            wrapped = ""
            col = 0
            for word in line.split(" "):
                if col + len(word) > self.wrap and col > 0:
                    wrapped += f"\n{prefix}"
                    col = 0
                if col > 0:
                    wrapped += " "
                    col += 1
                wrapped += word
                col += len(word)
            print(f"{prefix}{wrapped}", flush=True)
        else:
            print(f"{prefix}{line}", flush=True)
        self._emitted_first_line = True

    def _emit(self, text):
        self._line_buf += text
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            self._print_line(line)

    def _flush_emit(self):
        if self._line_buf:
            self._print_line(self._line_buf)
            self._line_buf = ""

    def _end_text(self):
        if self._in_text:
            self._flush_emit()
            sys.stdout.write(self.RESET + "\n")
            self._in_text = False
        if self._in_thinking:
            self._flush_emit()
            sys.stdout.write(self.RESET + "\n")
            self._in_thinking = False

    _TASK_INDENT = "    "

    def _print_task_detail(self, inp, output):
        prompt = inp.get("prompt", "")
        if prompt:
            lines = prompt.split("\n")
            for line in lines[:10]:
                print(f"{self._TASK_INDENT}{self.TOOL}{line}{self.RESET}", flush=True)
            if len(lines) > 10:
                print(
                    f"{self._TASK_INDENT}{self.TOOL}... ({len(lines) - 10} more lines){self.RESET}",
                    flush=True,
                )
        if not output:
            return
        body = re.sub(r"^task_id:.*\n*", "", output)
        body = re.sub(r"</?task_result>\n?", "", body).strip()
        if not body:
            return
        for line in body.split("\n"):
            print(f"{self._TASK_INDENT}{line}", flush=True)

    _GENERIC_ERROR = "Unexpected server error. Check server logs for details."

    def flush_errors(self):
        """Print collected errors, deduplicating generic messages."""
        if not self._errors:
            return
        specific = [e for e in self._errors if e != self._GENERIC_ERROR]
        if specific:
            for error_msg in dict.fromkeys(specific):
                print(
                    f"{self._INDENT}{self.RED}❌ Error: {error_msg}{self.RESET}",
                    flush=True,
                )
        else:
            print(
                f"{self._INDENT}{self.RED}❌ Error: OpenCode returned a server error. "
                f"Common causes: invalid model name, missing or expired credentials, "
                f"or insufficient API permissions.{self.RESET}",
                flush=True,
            )

    def process_line(self, line):
        """Process a single JSONL line from OpenCode. Returns True when run is complete."""
        line = line.strip()
        if not line:
            return False

        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False

        msg_type = msg.get("type")
        part = msg.get("part", {})

        if msg_type == "error":
            self._end_text()
            error = msg.get("error", {})
            error_msg = error.get("data", {}).get("message", str(error))
            self._errors.append(error_msg)
            return False

        if msg_type == "text":
            text = part.get("text", "")
            if not self._in_text:
                self._end_text()
                print(f"{self._INDENT}{self.AGENT}\U0001f4ac Agent ", end="", flush=True)
                self._in_text = True
                self._emitted_first_line = False
            self._emit(text + "\n")

        elif msg_type == "thinking":
            text = part.get("text", "")
            if not self._in_thinking:
                self._end_text()
                print(f"{self._INDENT}{self.THINK}\U0001f9e0 Thinking ", end="", flush=True)
                self._in_thinking = True
                self._emitted_first_line = False
            self._emit(text + "\n")

        elif msg_type == "tool_use":
            self._end_text()
            tool_name = part.get("tool", "unknown")
            display_name = tool_name[0].upper() + tool_name[1:] if tool_name else tool_name
            state = part.get("state", {})
            inp = state.get("input", {})
            title = part.get("title", "")
            summary = _format_tool(tool_name, inp) if inp else title
            icon = "\U0001f916" if tool_name in ("task", "agent") else "\U0001f527"
            print(
                f"{self._INDENT}{self.TOOL}{icon} {display_name} {summary}{self.RESET}",
                flush=True,
            )
            if tool_name in ("task", "agent"):
                self._print_task_detail(inp, state.get("output", ""))

        elif msg_type == "step_start":
            self._end_text()

        elif msg_type == "step_finish":
            self._end_text()
            reason = part.get("reason", "")
            tokens = part.get("tokens", {})
            cost = part.get("cost", 0)
            total = tokens.get("total", 0)
            inp = tokens.get("input", 0)
            out = tokens.get("output", 0)
            cache = tokens.get("cache", {})
            cache_r = cache.get("read", 0)
            cache_w = cache.get("write", 0)
            print(
                f"{self._INDENT}{self.TOOL}\U0001f4ca TOKENS in={inp} "
                f"out={out} "
                f"cache_r={cache_r} "
                f"cache_w={cache_w} "
                f"total={total} cost=${cost:.4f}{self.RESET}",
                flush=True,
            )
            if reason == "stop":
                return True

        return False

    def process(self, input_stream):
        """Process a stream of lines. Returns True if run completed normally."""
        for line in input_stream:
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            if self.process_line(line):
                return True
        return False


class CodexStreamProcessor:
    """Processes Codex CLI --json JSONL output and prints human-readable CI logs.

    Codex emits ``thread.started``, ``turn.*``, ``item.*``, and ``error``
    events. Items cover assistant messages, reasoning, command executions,
    file changes, MCP calls, web searches, plan updates, collab (sub-agent)
    tool calls, and ``error`` items, which carry non-fatal warnings such as
    missing model metadata.
    """

    def __init__(self, color=True, wrap=0, agent_pid=0):
        self.wrap = wrap
        self.agent_pid = agent_pid

        if color:
            self.THINK = "\033[3;36m"
            self.TOOL = "\033[1;90m"
            self.AGENT = ""
            self.RED = "\033[31m"
            self.WARN = "\033[33m"
            self.RESET = "\033[0m"
        else:
            self.THINK = self.TOOL = self.AGENT = self.RED = self.WARN = self.RESET = ""

        self._errors: list[str] = []
        self._started_items: set[str] = set()
        # Warning and failure lines already printed, keyed by (label, message),
        # so repeats collapse into one line plus a suppressed count.
        self._notices: set[tuple[str, str]] = set()
        self._suppressed_notices = 0
        # Collab tool calls (spawn_agent, wait, ...) that started but have not
        # completed, by item id -> tool name.
        self._open_collab: dict[str, str] = {}
        # Usage reported by nested ``codex exec`` runs the agent launched from
        # shell commands, measured as non-cached input + output tokens.
        self._nested_runs = 0
        self._nested_tokens = 0
        self._summary_printed = False

    _INDENT = "  "
    # At most this many distinct warning/failure lines are printed; the rest
    # are counted and reported in one summary line.
    _MAX_NOTICES = 10
    # Sandbox-provided messages are flattened to one line and capped.
    _MAX_NOTICE_CHARS = 200
    # Collab tool names come from the sandbox too and get a tighter cap.
    _MAX_TOOL_CHARS = 60
    # A nested ``codex exec`` run in human mode starts with this header line
    # and ends with "tokens used\n<n>".
    _NESTED_HEADER_RE = re.compile(r"^OpenAI Codex v", re.M)
    _NESTED_TOKENS_RE = re.compile(r"^tokens used[ \t]*\r?\n[ \t]*(\d[\d,]*)[ \t]*\r?$", re.M)
    # One ``codex [options] exec`` (or ``e``) invocation in a shell command.
    # Paths such as ``/tmp/codex-impl.log`` do not match.
    _CODEX_EXEC_RE = re.compile(
        r"(?:^|[\s;&|(\"'`])(?:[^\s/;&|]*/)*codex(?:\s+[^\s;&|]+)*?\s+(?:exec|e)(?=[\s\"']|$)"
    )
    _JSON_FLAG_RE = re.compile(r"(?<!\S)--(?:experimental-)?json(?![\w-])")

    def _print_text(self, label, text, style=""):
        lines = str(text).splitlines() or [""]
        first_prefix = f"{self._INDENT}{style}{label}"
        # Width must be measured against visible characters only; the ANSI
        # ``style`` escape is non-printing, so excluding it keeps wrapping at
        # the intended column instead of wrapping early when color is enabled.
        first_prefix_width = len(self._INDENT) + len(label)
        continuation = self._INDENT * 2
        for index, line in enumerate(lines):
            prefix = first_prefix if index == 0 else continuation
            prefix_width = first_prefix_width if index == 0 else len(continuation)
            if self.wrap and prefix_width + len(line) > self.wrap:
                available = max(self.wrap - len(continuation), 20)
                words = line.split()
                chunks = []
                current = ""
                for word in words:
                    candidate = f"{current} {word}".strip()
                    if current and len(candidate) > available:
                        chunks.append(current)
                        current = word
                    else:
                        current = candidate
                chunks.append(current)
                for chunk_index, chunk in enumerate(chunks):
                    chunk_prefix = prefix if chunk_index == 0 else continuation
                    print(f"{chunk_prefix}{chunk}{self.RESET}", flush=True)
            else:
                print(f"{prefix}{line}{self.RESET}", flush=True)

    @staticmethod
    def _error_message(error):
        if isinstance(error, dict):
            return error.get("message", str(error))
        return str(error)

    def _print_command(self, item):
        command = item.get("command", "")
        self._print_text("\U0001f527 Shell $ ", command, self.TOOL)

    @classmethod
    def _one_line(cls, text, limit=None):
        """Collapse whitespace and cap length for a single log line."""
        limit = limit or cls._MAX_NOTICE_CHARS
        flat = " ".join(str(text).split())
        if len(flat) > limit:
            flat = flat[: limit - 1].rstrip() + "…"
        return flat

    def _notice(self, label, message, style):
        """Print a warning or failure line once; count repeats and overflow."""
        message = self._one_line(message)
        key = (label, message)
        if key in self._notices or len(self._notices) >= self._MAX_NOTICES:
            self._suppressed_notices += 1
            return
        self._notices.add(key)
        self._print_text(label, message, style)

    @classmethod
    def _collab_tool(cls, item):
        return cls._one_line(item.get("tool") or "collab_tool_call", cls._MAX_TOOL_CHARS)

    @staticmethod
    def _collab_failure(item):
        """Return (failed, detail) for a completed collab tool call."""
        errored = []
        states = item.get("agents_states")
        if isinstance(states, dict):
            for state in states.values():
                if isinstance(state, dict) and state.get("status") == "errored":
                    errored.append(state.get("message") or "")
        failed = item.get("status") == "failed" or bool(errored)
        detail = next((m for m in errored if m), "")
        return failed, detail

    def _collect_nested_usage(self, item):
        """Add token usage of ``codex exec`` runs launched from a shell command.

        Codex does not report sub-run usage in the parent turn.completed. A
        nested run in human mode prints a header line and, at the end, "tokens
        used" followed by its non-cached input + output total; a nested
        ``--json`` run prints its own ``thread.started`` and ``turn.completed``
        events. Only commands that invoke ``codex ... exec`` are considered,
        and at most one run is counted per invocation in the command, so text
        the nested agent printed (for example a saved log it cat'ed) cannot
        add runs of its own.
        """
        command = item.get("command", "")
        output = item.get("aggregated_output", "")
        if not isinstance(command, str) or not isinstance(output, str):
            return
        invocations = len(self._CODEX_EXEC_RE.findall(command))
        if not invocations:
            return
        if self._NESTED_HEADER_RE.search(output):
            runs = self._human_run_usage(output)
        elif self._JSON_FLAG_RE.search(command):
            runs = self._json_run_usage(output)
        else:
            return
        # A run's own total is the last one it prints, so keep the last values.
        runs = runs[-invocations:]
        self._nested_runs += len(runs)
        self._nested_tokens += sum(runs)

    @classmethod
    def _human_run_usage(cls, output):
        """Last "tokens used" value per header-delimited segment of output."""
        starts = [m.start() for m in cls._NESTED_HEADER_RE.finditer(output)]
        bounds = zip(starts, starts[1:] + [len(output)])
        runs = []
        for start, end in bounds:
            matches = cls._NESTED_TOKENS_RE.findall(output, start, end)
            if matches:
                runs.append(int(matches[-1].replace(",", "")))
        return runs

    @classmethod
    def _json_run_usage(cls, output):
        """Usage of the first turn.completed after each thread.started line."""
        runs = []
        awaiting_turn = False
        for raw in output.splitlines():
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                awaiting_turn = True
            elif event_type == "turn.completed" and awaiting_turn:
                usage = event.get("usage")
                if isinstance(usage, dict):
                    runs.append(cls._used_tokens(usage))
                    awaiting_turn = False
        return runs

    @staticmethod
    def _used_tokens(usage):
        """Non-cached input + output tokens, matching Codex's "tokens used"."""
        try:
            inp = int(usage.get("input_tokens", 0) or 0)
            cached = int(usage.get("cached_input_tokens", 0) or 0)
            out = int(usage.get("output_tokens", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return max(inp - cached, 0) + max(out, 0)

    def _print_run_summary(self):
        """Report unfinished collab calls and suppressed notices, once."""
        if self._summary_printed:
            return
        self._summary_printed = True
        unfinished: dict[str, int] = {}
        for tool in self._open_collab.values():
            unfinished[tool] = unfinished.get(tool, 0) + 1
        for tool, count in unfinished.items():
            # Codex exec emits no item.completed for a collab call that failed
            # to start or was interrupted (the Interrupted status has no JSONL
            # mapping), so the stream cannot tell the two apart.
            calls = "call" if count == 1 else "calls"
            self._print_text(
                f"⚠️ Codex {tool}: ",
                f"{count} {calls} never completed (failed or interrupted; "
                f"the stream carries no result)",
                self.WARN,
            )
        if self._suppressed_notices:
            self._print_text(
                "⚠️ ",
                f"{self._suppressed_notices} repeated or additional Codex warning/"
                f"failure line(s) suppressed",
                self.WARN,
            )

    @staticmethod
    def _mcp_name(item):
        server = item.get("server", "")
        tool = item.get("tool", item.get("name", "unknown"))
        return f"{server}/{tool}" if server else tool

    def _process_item(self, event_type, item):
        item_id = item.get("id", "")
        item_type = item.get("type", "")

        if event_type == "item.started":
            if item_id:
                self._started_items.add(item_id)
            if item_type == "command_execution":
                self._print_command(item)
            elif item_type == "mcp_tool_call":
                self._print_text("\U0001f527 MCP ", self._mcp_name(item), self.TOOL)
            elif item_type == "web_search":
                self._print_text("\U0001f50d Web search ", item.get("query", ""), self.TOOL)
            elif item_type == "collab_tool_call":
                tool = self._collab_tool(item)
                self._open_collab[item_id] = tool
                # Only spawns get a start line; wait and friends are polled in
                # loops and would flood the log.
                if tool == "spawn_agent":
                    self._print_text("\U0001f916 Agent ", tool, self.TOOL)
            return

        if item_type == "agent_message":
            self._print_text("\U0001f4ac Codex ", item.get("text", ""), self.AGENT)
        elif item_type == "reasoning":
            text = item.get("text", item.get("summary", ""))
            if text:
                self._print_text("\U0001f9e0 Thinking ", text, self.THINK)
        elif item_type == "command_execution":
            if item_id not in self._started_items:
                self._print_command(item)
            exit_code = item.get("exit_code")
            # Codex may serialize exit_code as a number or a numeric string;
            # normalize before comparing so a successful "0" is not rendered
            # as a failure.
            try:
                normalized = int(exit_code) if exit_code is not None else None
            except (TypeError, ValueError):
                normalized = exit_code
            if normalized not in (None, 0):
                self._print_text("  exit=", str(exit_code), self.RED)
            self._collect_nested_usage(item)
        elif item_type == "file_change":
            changes = item.get("changes", [])
            if changes:
                for change in changes:
                    if isinstance(change, dict):
                        path = change.get("path", "")
                        kind = change.get("kind", change.get("type", ""))
                        detail = f"{kind} {path}".strip()
                    else:
                        detail = str(change)
                    self._print_text("\U0001f527 File change ", detail, self.TOOL)
            else:
                self._print_text("\U0001f527 File change ", item.get("path", ""), self.TOOL)
        elif item_type == "mcp_tool_call":
            error = item.get("error")
            if error or item.get("status") in ("failed", "error"):
                message = self._error_message(error or "MCP tool call failed")
                label = f"❌ MCP {self._mcp_name(item)} failed: "
                self._print_text(label, message, self.RED)
        elif item_type == "plan":
            text = item.get("text", item.get("plan", ""))
            if text:
                self._print_text("\U0001f4cb Plan ", text, self.TOOL)
        elif item_type == "error":
            # Codex reports non-fatal warnings (missing model metadata, config
            # and deprecation warnings, model reroutes) as error items.
            message = item.get("message") or "unknown warning"
            self._notice("⚠️ Codex warning: ", message, self.WARN)
        elif item_type == "collab_tool_call":
            self._open_collab.pop(item_id, None)
            failed, detail = self._collab_failure(item)
            if failed:
                tool = self._collab_tool(item)
                label = f"❌ Codex {tool} failed: "
                self._notice(label, detail or "no error detail in stream", self.RED)

    def flush_errors(self):
        """Print the run summary (if not yet printed) and collected errors."""
        self._print_run_summary()
        if not self._errors:
            return
        for error_msg in dict.fromkeys(self._errors):
            print(
                f"{self._INDENT}{self.RED}❌ Error: {error_msg}{self.RESET}",
                flush=True,
            )

    def process_line(self, line):
        """Process a single JSONL line from Codex. Returns True when run is complete."""
        line = line.strip()
        if not line:
            return False

        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False

        msg_type = msg.get("type", "")

        if msg_type == "error":
            error_msg = msg.get("message", self._error_message(msg.get("error", "unknown error")))
            self._errors.append(error_msg)
            return False

        if msg_type in ("item.started", "item.completed"):
            self._process_item(msg_type, msg.get("item", {}))
            return False

        if msg_type == "turn.failed":
            self._errors.append(self._error_message(msg.get("error", "Codex turn failed")))
            return False

        if msg_type == "turn.completed":
            # ``codex exec`` runs a single turn per invocation and reports its
            # terminal token usage here, so the first turn.completed marks the
            # end of the run. Returning True is load-bearing for telemetry:
            # backend._process_stream uses it to wait for the OTEL batch
            # exporter to flush (otherwise the root span is often lost) and to
            # promote a post-completion non-zero exit code to success. If a
            # future Codex exec mode emits multiple turns, completion should be
            # keyed off a thread-level terminal event instead of this first
            # turn.completed to avoid truncating the run.
            usage = msg.get("usage", {})
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            cache_r = usage.get("cached_input_tokens", 0)
            cache_w = usage.get("cache_write_input_tokens", 0)
            total = inp + out
            self._print_run_summary()
            print(
                f"{self._INDENT}{self.TOOL}\U0001f4ca TOKENS in={inp} "
                f"out={out} cache_r={cache_r} cache_w={cache_w} "
                f"total={total}{self.RESET}",
                flush=True,
            )
            if self._nested_runs:
                # Nested runs only report non-cached input + output, so the
                # combined figure uses that measure for the parent as well.
                parent_used = self._used_tokens(usage)
                print(
                    f"{self._INDENT}{self.TOOL}\U0001f4ca TOKENS nested_runs="
                    f"{self._nested_runs} nested_used={self._nested_tokens} "
                    f"parent_used={parent_used} "
                    f"combined_used={parent_used + self._nested_tokens} "
                    f"(used = non-cached input + output){self.RESET}",
                    flush=True,
                )
            return True

        return False

    def process(self, input_stream):
        """Process a stream of lines. Returns True if run completed normally."""
        for line in input_stream:
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            if self.process_line(line):
                return True
        return False
