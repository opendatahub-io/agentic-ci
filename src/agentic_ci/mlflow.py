"""Push OTel trace payloads from a JSONL log to an MLflow OTLP endpoint."""

import json
import ssl
import sys
import urllib.error
import urllib.request


def _resolve_experiment_id(endpoint, name, headers):
    """Look up an MLflow experiment by name. Returns ID or None."""
    url = f"{endpoint}/api/2.0/mlflow/experiments/search"
    escaped_name = name.replace("'", "''")
    body = json.dumps({"filter": f"name = '{escaped_name}'"}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context()) as resp:
            data = json.loads(resp.read())
            experiments = data.get("experiments", [])
            if experiments:
                return experiments[0]["experiment_id"]
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass
    return None


def push_traces(log_file, endpoint, experiment, token=None):
    """Push /v1/traces records from a JSONL log to an MLflow OTLP endpoint.

    Returns (ok_count, error_count).
    """
    endpoint = endpoint.rstrip("/")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    records = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if "/v1/traces" in rec.get("path", ""):
                        records.append(rec)
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        return 0, 0

    if not records:
        return 0, 0

    experiment_id = _resolve_experiment_id(endpoint, experiment, headers)
    if not experiment_id:
        print(
            f"MLflow experiment '{experiment}' not found — skipping trace push.",
            file=sys.stderr,
        )
        return 0, 0

    ctx = ssl.create_default_context()
    traces_url = f"{endpoint}/v1/traces"
    ok, err = 0, 0

    for rec in records:
        payload = rec.get("payload")
        if not payload:
            continue
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            traces_url,
            data=body,
            headers={
                **headers,
                "Content-Type": "application/json",
                "x-mlflow-experiment-id": experiment_id,
            },
        )
        try:
            with urllib.request.urlopen(req, context=ctx) as resp:
                resp.read()
                ok += 1
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                detail = f" ({e.code})"
            print(f"Trace push failed{detail}: {e}", file=sys.stderr)
            err += 1

    return ok, err
