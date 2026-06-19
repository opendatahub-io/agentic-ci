#!/usr/bin/env python3
"""Probe HTTPS connectivity through the OpenShell proxy to detect hangs.

Makes repeated HTTPS requests to policy-allowed endpoints and logs
timing.  Designed to run *inside* an OpenShell sandbox to reproduce
proxy hang / timeout issues (e.g. connections dropping during token
rotation or L4 CONNECT tunnel idle timeouts).

Runs with stdlib only so it works in minimal sandbox images.

Usage (inside sandbox):
    python3 proxy-probe.py                        # rapid mode, 2h
    python3 proxy-probe.py --duration 300         # quick 5-min run
    python3 proxy-probe.py --mode sustained       # hold connections open
    python3 proxy-probe.py --vertex               # include Vertex AI calls
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

# ── Probe targets (must be in OpenShell default policy) ──────────────

TARGETS = [
    {"name": "github", "url": "https://api.github.com/zen"},
    {"name": "pypi", "url": "https://pypi.org/simple/pip/"},
    {"name": "gitlab", "url": "https://gitlab.com/api/v4/version"},
]


# ── Helpers ──────────────────────────────────────────────────────────

def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _log(elapsed_total: float, marker: str, target: str, elapsed_s: float,
         status: int | None, error: str | None) -> None:
    err = f" {error}" if error else ""
    print(
        f"[{elapsed_total:8.1f}s] {_ts()} {marker:4s} "
        f"{target:12s} {elapsed_s:7.3f}s "
        f"status={status}{err}",
        flush=True,
    )


# ── Rapid mode ───────────────────────────────────────────────────────

def probe_http(url: str, timeout: int = 30) -> tuple[float, int | None, str | None]:
    """Make one HTTPS request.  Returns (elapsed_s, status, error)."""
    start = time.monotonic()
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "agentic-ci-proxy-probe/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp.read(1024)
            return time.monotonic() - start, resp.status, None
    except urllib.error.HTTPError as exc:
        return time.monotonic() - start, exc.code, str(exc)
    except urllib.error.URLError as exc:
        return time.monotonic() - start, None, f"URLError: {exc.reason}"
    except (TimeoutError, socket.timeout):
        return time.monotonic() - start, None, "TIMEOUT"
    except OSError as exc:
        return time.monotonic() - start, None, f"OSError: {exc}"


def probe_vertex(project: str, region: str, timeout: int = 60,
                 ) -> tuple[float, int | None, str | None]:
    """Minimal Vertex AI Claude call (max_tokens=1)."""
    token = os.environ.get("GCP_SA_ACCESS_TOKEN", "")
    if not token:
        return 0.0, None, "GCP_SA_ACCESS_TOKEN not set"

    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{region}/"
        "publishers/anthropic/models/claude-sonnet-4-20250514:rawPredict"
    )
    body = json.dumps({
        "anthropic_version": "vertex-2023-10-16",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    start = time.monotonic()
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp.read()
            return time.monotonic() - start, resp.status, None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            pass
        return time.monotonic() - start, exc.code, f"HTTP {exc.code}: {detail}"
    except urllib.error.URLError as exc:
        return time.monotonic() - start, None, f"URLError: {exc.reason}"
    except (TimeoutError, socket.timeout):
        return time.monotonic() - start, None, "TIMEOUT"
    except OSError as exc:
        return time.monotonic() - start, None, f"OSError: {exc}"


def run_rapid(duration: int, interval: int, vertex: bool) -> int:
    """Rapid mode: short requests in a loop."""
    project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "us-east5")

    t0 = time.monotonic()
    total = failures = slow = hangs = 0
    max_latency = 0.0

    while time.monotonic() - t0 < duration:
        cycle_start = time.monotonic()
        elapsed_total = cycle_start - t0

        for tgt in TARGETS:
            elapsed_s, status, error = probe_http(tgt["url"])
            total += 1
            max_latency = max(max_latency, elapsed_s)
            ok = status is not None and 200 <= status < 400
            if not ok:
                failures += 1
            if elapsed_s > 5:
                slow += 1
            if elapsed_s > 30:
                hangs += 1
            marker = "FAIL" if not ok else ("SLOW" if elapsed_s > 5 else "OK")
            _log(elapsed_total, marker, tgt["name"], elapsed_s, status, error)

        if vertex and project:
            elapsed_s, status, error = probe_vertex(project, region)
            total += 1
            max_latency = max(max_latency, elapsed_s)
            ok = status == 200
            if not ok:
                failures += 1
            if elapsed_s > 5:
                slow += 1
            if elapsed_s > 30:
                hangs += 1
            marker = "FAIL" if not ok else ("SLOW" if elapsed_s > 5 else "OK")
            _log(elapsed_total, marker, "vertex", elapsed_s, status, error)

        wait = interval - (time.monotonic() - cycle_start)
        if wait > 0:
            time.sleep(wait)

    _print_summary(time.monotonic() - t0, total, failures, slow, hangs, max_latency)
    return 1 if failures or hangs else 0


# ── Sustained mode ───────────────────────────────────────────────────

def run_sustained(duration: int, interval: int) -> int:
    """Sustained mode: hold HTTPS connections open for long periods.

    Tests whether the L4 CONNECT tunnel drops idle or slow connections.
    Opens a TLS connection to api.github.com, sends an HTTP request,
    reads one byte at a time with sleeps to simulate a slow consumer.
    """
    t0 = time.monotonic()
    total = failures = 0

    print(f"Sustained mode: holding connections for {interval}s each", flush=True)

    while time.monotonic() - t0 < duration:
        elapsed_total = time.monotonic() - t0
        target = "github"
        start = time.monotonic()

        try:
            ctx = ssl.create_default_context()
            raw = socket.create_connection(("api.github.com", 443), timeout=30)
            conn = ctx.wrap_socket(raw, server_hostname="api.github.com")

            conn.sendall(
                b"GET /zen HTTP/1.1\r\n"
                b"Host: api.github.com\r\n"
                b"User-Agent: agentic-ci-proxy-probe/1.0\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )

            # Read response headers
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(1)
                if not chunk:
                    raise ConnectionError("connection closed during headers")
                buf += chunk

            # Now hold the connection open, reading slowly
            hold_start = time.monotonic()
            bytes_read = 0
            while time.monotonic() - hold_start < interval:
                conn.settimeout(5.0)
                try:
                    data = conn.recv(1)
                    if data:
                        bytes_read += len(data)
                except socket.timeout:
                    pass
                time.sleep(1)

            conn.close()
            elapsed_s = time.monotonic() - start
            total += 1
            _log(elapsed_total, "OK", target, elapsed_s, 200,
                 f"held {elapsed_s:.0f}s, read {bytes_read}B")

        except Exception as exc:
            elapsed_s = time.monotonic() - start
            error = f"{type(exc).__name__}: {exc}"
            total += 1
            failures += 1
            _log(elapsed_total, "FAIL", target, elapsed_s, None, error)

    _print_summary(time.monotonic() - t0, total, failures, 0, 0, 0)
    return 1 if failures else 0


# ── Summary ──────────────────────────────────────────────────────────

def _print_summary(wall: float, total: int, failures: int,
                   slow: int, hangs: int, max_latency: float) -> None:
    pct = f"{failures / total * 100:.1f}%" if total else "N/A"
    print(flush=True)
    print("=" * 60, flush=True)
    print("PROBE SUMMARY", flush=True)
    print(f"  Wall time:     {wall:.1f}s", flush=True)
    print(f"  Requests:      {total}", flush=True)
    print(f"  Failures:      {failures} ({pct})", flush=True)
    if slow:
        print(f"  Slow (>5s):    {slow}", flush=True)
    if hangs:
        print(f"  Hangs (>30s):  {hangs}", flush=True)
    if max_latency:
        print(f"  Max latency:   {max_latency:.3f}s", flush=True)
    print("=" * 60, flush=True)


# ── Main ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe HTTPS connectivity through the OpenShell proxy",
    )
    parser.add_argument(
        "--duration", type=int, default=7200,
        help="Total run duration in seconds (default: 7200 = 2h)",
    )
    parser.add_argument(
        "--interval", type=int, default=10,
        help="Seconds between probe cycles in rapid mode, or hold "
             "duration in sustained mode (default: 10)",
    )
    parser.add_argument(
        "--mode", choices=["rapid", "sustained"], default="rapid",
        help="rapid = short requests; sustained = hold connections open",
    )
    parser.add_argument(
        "--vertex", action="store_true",
        help="Include Vertex AI API probes (needs GCP credentials)",
    )
    args = parser.parse_args()

    print(f"proxy-probe  mode={args.mode}  duration={args.duration}s  "
          f"interval={args.interval}s  vertex={args.vertex}", flush=True)
    print(flush=True)

    if args.mode == "rapid":
        rc = run_rapid(args.duration, args.interval, args.vertex)
    else:
        rc = run_sustained(args.duration, args.interval)
    sys.exit(rc)


if __name__ == "__main__":
    main()
