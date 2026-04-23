"""Parse OTLP JSONL log and print a human-readable token/cost summary."""

import json
import sys
from collections import defaultdict


def parse_metrics(records):
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
            print(f"  {'-'*20} {'-'*12}")
            model_tokens = {t: c for (m, t), c in token_totals.items() if m == model}
            for token_type in ["input", "cacheRead", "cacheCreation", "output"]:
                if token_type in model_tokens:
                    print(f"  {token_type:<20} {model_tokens[token_type]:>12,.0f}")
            total = sum(model_tokens.values())
            print(f"  {'TOTAL':<20} {total:>12,.0f}")

    if cost_totals:
        print(f"\n  {'Model':<30} {'Cost (USD)':>12}")
        print(f"  {'-'*30} {'-'*12}")
        grand_total = 0.0
        for model in sorted(cost_totals.keys()):
            cost = cost_totals[model]
            grand_total += cost
            print(f"  {model:<30} ${cost:>11.4f}")
        if len(cost_totals) > 1:
            print(f"  {'TOTAL':<30} ${grand_total:>11.4f}")

    if active_time:
        print(f"\n  Active Time:")
        for time_type, seconds in sorted(active_time.items()):
            mins, secs = divmod(int(seconds), 60)
            print(f"    {time_type}: {mins}m {secs}s")

    if api_requests:
        print(f"\n  API Requests: {len(api_requests)}")
        total_duration = sum(float(r.get("duration_ms", 0)) for r in api_requests)
        if total_duration:
            print(f"  Total API time: {total_duration/1000:.1f}s")

    print("=" * 60)


def main(args=None):
    import argparse

    parser = argparse.ArgumentParser(description="Print Claude OTEL token/cost summary")
    parser.add_argument("log_file", nargs="?", default="/tmp/claude-otel.jsonl",
                        help="Path to OTEL JSONL log file")
    parsed = parser.parse_args(args)
    print_summary(parsed.log_file)


if __name__ == "__main__":
    main()
