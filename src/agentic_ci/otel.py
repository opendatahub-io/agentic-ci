"""OTLP HTTP/JSON receiver and token/cost summary.

Lightweight collector that accepts OTLP exports for metrics and logs,
tracks token usage over a sliding window, and prints a summary.
"""

import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

_token_samples = []
_WINDOW_SECS = 60


MAX_BODY_SIZE = 1_048_576


class OTLPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if length > MAX_BODY_SIZE:
            self.send_error(413, "Payload Too Large")
            return
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"raw": body.decode("utf-8", errors="replace")}

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "path": self.path,
            "payload": payload,
        }
        log_file = os.environ.get("OTEL_LOG_FILE", "/tmp/claude-otel.jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        if "/v1/metrics" in self.path:
            _update_token_rate(payload)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"partialSuccess":{}}')

    def log_message(self, format, *args):
        pass


def _update_token_rate(payload):
    global _token_samples
    now = time.monotonic()
    total = 0
    for rm in payload.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                if metric.get("name") == "claude_code.token.usage":
                    data = metric.get("sum", metric.get("gauge", {}))
                    for dp in data.get("dataPoints", []):
                        total += dp.get("asDouble", dp.get("asInt", 0))
    if total <= 0:
        return

    _token_samples.append((now, total))
    cutoff = now - _WINDOW_SECS
    _token_samples = [(t, v) for t, v in _token_samples if t >= cutoff]

    rate = 0.0
    if len(_token_samples) >= 2:
        dt = _token_samples[-1][0] - _token_samples[0][0]
        dv = _token_samples[-1][1] - _token_samples[0][1]
        if dt > 0:
            rate = dv / dt

    rate_file = os.environ.get("OTEL_RATE_FILE", "/tmp/claude-otel-rate.json")
    tmp = rate_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"total": total, "rate": rate, "ts": time.time()}, f)
    os.replace(tmp, rate_file)


def start_collector(run_dir):
    """Start the OTEL collector as a subprocess. Returns (proc, port)."""
    otel_log = os.path.join(run_dir, "claude-otel.jsonl")
    otel_rate = os.path.join(run_dir, "claude-otel-rate.json")
    port_file = os.path.join(run_dir, "otel-port")

    for f in [otel_log, port_file]:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass

    env = {
        **os.environ,
        "OTEL_LOG_FILE": otel_log,
        "OTEL_RATE_FILE": otel_rate,
        "OTEL_COLLECTOR_PORT": "0",
        "OTEL_PORT_FILE": port_file,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_ci.otel"],
        env=env,
    )

    for _ in range(50):
        if os.path.exists(port_file):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("OTEL collector did not write port file")

    with open(port_file) as f:
        port = int(f.read().strip())

    return proc, port, otel_log, otel_rate


def stop_collector(proc):
    """Stop the OTEL collector subprocess."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def parse_metrics(records):
    """Parse OTLP JSONL records into structured token/cost data."""
    token_totals = defaultdict(float)
    cost_totals = defaultdict(float)
    api_requests = []
    active_time = defaultdict(float)

    for rec in records:
        path = rec.get("path", "")
        payload = rec.get("payload", {})

        if "/v1/metrics" in path:
            for rm in payload.get("resourceMetrics", []):
                for sm in rm.get("scopeMetrics", []):
                    for metric in sm.get("metrics", []):
                        name = metric.get("name", "")
                        data = metric.get("sum", metric.get("gauge", metric.get("histogram", {})))
                        for dp in data.get("dataPoints", []):
                            attrs = {
                                a["key"]: a["value"].get(
                                    "stringValue",
                                    a["value"].get("intValue", a["value"].get("doubleValue")),
                                )
                                for a in dp.get("attributes", [])
                            }
                            value = dp.get("asDouble", dp.get("asInt", 0))

                            if name == "claude_code.token.usage":
                                model = attrs.get("model", "unknown")
                                token_type = attrs.get("type", "unknown")
                                token_totals[(model, token_type)] += value
                            elif name == "claude_code.cost.usage":
                                model = attrs.get("model", "unknown")
                                cost_totals[model] += value
                            elif name == "claude_code.active_time.total":
                                time_type = attrs.get("type", "unknown")
                                active_time[time_type] += value

        elif "/v1/logs" in path:
            for rl in payload.get("resourceLogs", []):
                for sl in rl.get("scopeLogs", []):
                    for lr in sl.get("logRecords", []):
                        event_name = ""
                        event_attrs = {}
                        for a in lr.get("attributes", []):
                            key = a["key"]
                            val = a["value"]
                            v = val.get("stringValue", val.get("intValue", val.get("doubleValue")))
                            event_attrs[key] = v
                            if key == "event.name":
                                event_name = v
                        if event_name == "claude_code.api_request":
                            api_requests.append(event_attrs)

    return token_totals, cost_totals, api_requests, active_time


def print_summary(log_file):
    """Print a human-readable token/cost summary from an OTEL JSONL log."""
    records = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except FileNotFoundError:
        print("No OTEL data collected (log file not found).")
        return
    except json.JSONDecodeError as e:
        print(f"Error parsing OTEL log: {e}")
        return

    if not records:
        print("No OTEL data collected.")
        return

    token_totals, cost_totals, api_requests, active_time = parse_metrics(records)

    print("=" * 60)
    print("  CLAUDE TOKEN & COST SUMMARY (OpenTelemetry)")
    print("=" * 60)

    if token_totals:
        models = sorted(set(m for m, _ in token_totals.keys()))
        for model in models:
            print(f"\n  Model: {model}")
            print(f"  {'Token Type':<20} {'Count':>12}")
            print(f"  {'-' * 20} {'-' * 12}")
            model_tokens = {t: c for (m, t), c in token_totals.items() if m == model}
            for token_type in ["input", "cacheRead", "cacheCreation", "output"]:
                if token_type in model_tokens:
                    print(f"  {token_type:<20} {model_tokens[token_type]:>12,.0f}")
            total = sum(model_tokens.values())
            print(f"  {'TOTAL':<20} {total:>12,.0f}")

    if cost_totals:
        print(f"\n  {'Model':<30} {'Cost (USD)':>12}")
        print(f"  {'-' * 30} {'-' * 12}")
        grand_total = 0.0
        for model in sorted(cost_totals.keys()):
            cost = cost_totals[model]
            grand_total += cost
            print(f"  {model:<30} ${cost:>11.4f}")
        if len(cost_totals) > 1:
            print(f"  {'TOTAL':<30} ${grand_total:>11.4f}")

    if active_time:
        print("\n  Active Time:")
        for time_type, seconds in sorted(active_time.items()):
            mins, secs = divmod(int(seconds), 60)
            print(f"    {time_type}: {mins}m {secs}s")

    if api_requests:
        print(f"\n  API Requests: {len(api_requests)}")
        total_duration = sum(float(r.get("duration_ms", 0)) for r in api_requests)
        if total_duration:
            print(f"  Total API time: {total_duration / 1000:.1f}s")

    print("=" * 60)


def main():
    """Run the OTEL collector server."""
    port = int(os.environ.get("OTEL_COLLECTOR_PORT", "4318"))
    server = HTTPServer(("127.0.0.1", port), OTLPHandler)
    actual_port = server.server_address[1]
    port_file = os.environ.get("OTEL_PORT_FILE")
    if port_file:
        with open(port_file, "w") as f:
            f.write(str(actual_port))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log_file = os.environ.get("OTEL_LOG_FILE", "/tmp/claude-otel.jsonl")
    print(
        f"OTLP collector listening on 127.0.0.1:{actual_port}, writing to {log_file}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
