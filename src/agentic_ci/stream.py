"""Parse Claude Code stream-json output into a human-readable CI log."""

import json
import os
import signal
import sys
import time


def _format_tool(name, params):
    """Return a compact one-line summary for known tools."""
    if name == "Bash":
        cmd = params.get("command", "")
        desc = params.get("description", "")
        return f"$ {cmd}" + (f"  # {desc}" if desc else "")
    if name == "Read":
        path = params.get("file_path", "")
        parts = [path]
        if "offset" in params:
            parts.append(f"L{params['offset']}")
        if "limit" in params:
            parts.append(f"+{params['limit']}")
        return " ".join(parts)
    if name == "Write":
        return params.get("file_path", "")
    if name == "Edit":
        path = params.get("file_path", "")
        old = params.get("old_string", "")
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
    if name == "Agent":
        desc = params.get("description", "")
        agent_type = params.get("subagent_type", "")
        return f"[{agent_type}] {desc}" if agent_type else desc
    if name == "Skill":
        skill = params.get("skill", "")
        skill_args = params.get("args", "")
        return f"/{skill} {skill_args}".strip()
    if name == "TaskGet":
        return params.get("task_id", "")
    return ", ".join(f"{k}={v}" for k, v in params.items())


class StreamProcessor:
    """Processes Claude Code stream-json and prints human-readable output."""

    def __init__(self, color=True, wrap=0, claude_pid=0):
        self.wrap = wrap
        self.claude_pid = claude_pid

        if color:
            self.THINK = "\033[3;31m"
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
            if subtype == "api_retry":
                attempt = msg.get("attempt", "?")
                max_retries = msg.get("max_retries", "?")
                delay = msg.get("retry_delay_ms", "?")
                error = msg.get("error", "unknown")
                self._last_block_type = "system"
                print(
                    f"{self.YELLOW}\U0001f504 Retry {attempt}/{max_retries}{self.RESET} "
                    f"{error} — retrying in {delay}ms",
                    flush=True,
                )
            return False

        if msg_type == "user":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, str) and "FULL RUN COMPLETE" in content:
                        self._end_block()
                        if self.claude_pid:
                            print("--- FULL RUN COMPLETE detected, terminating Claude ---",
                                  flush=True)
                            os.kill(self.claude_pid, signal.SIGTERM)
                        return True
            return False

        if msg_type == "result":
            self._end_block()
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
                print(f"{self.CLAUDE}\U0001f4ac Claude ", end="", flush=True)
                self._in_text = True
            elif block_type == "thinking":
                self._last_block_type = "thinking"
                print(f"{self.THINK}\U0001f9e0 Thinking ", end="", flush=True)
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
                total = (self._total_input + self._total_output
                         + self._total_cache_read + self._total_cache_write)
                if total - self._last_emitted_total >= 5_000 or self._last_emitted_total == 0:
                    now = time.monotonic()
                    rate = 0.0
                    try:
                        with open(os.environ.get("OTEL_RATE_FILE", "/tmp/claude-otel-rate.json")) as rf:
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
                f"{self.RED}❌ Error: {error_type}: {error_msg}{self.RESET}",
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


def main(args=None):
    import argparse

    parser = argparse.ArgumentParser(description="Parse Claude Code stream-json output")
    parser.add_argument("--wrap", type=int, default=0, metavar="COLS",
                        help="Word-wrap output at COLS columns (0 = no wrapping)")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color codes in output")
    parser.add_argument("--claude-pid", type=int, default=0,
                        help="PID of Claude Code process to kill on completion")
    parsed = parser.parse_args(args)

    processor = StreamProcessor(
        color=not parsed.no_color,
        wrap=parsed.wrap,
        claude_pid=parsed.claude_pid,
    )
    processor.process(sys.stdin)
    print()


if __name__ == "__main__":
    main()
