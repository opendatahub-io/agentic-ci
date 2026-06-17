"""Push OTel trace payloads from a JSONL log to an MLflow OTLP endpoint."""

import json
import sys

import requests


def _resolve_experiment_id(endpoint, name, headers):
    """Look up an MLflow experiment by name. Returns ID or None."""
    url = f"{endpoint}/api/2.0/mlflow/experiments/search"
    escaped_name = name.replace("'", "''")
    try:
        resp = requests.post(
            url,
            json={"filter": f"name = '{escaped_name}'", "max_results": 1},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        experiments = resp.json().get("experiments", [])
        if experiments:
            return experiments[0]["experiment_id"]
    except requests.RequestException:
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

    traces_url = f"{endpoint}/v1/traces"
    ok, err = 0, 0

    for rec in records:
        payload = rec.get("payload")
        if not payload:
            continue
        try:
            resp = requests.post(
                traces_url,
                json=payload,
                headers={
                    **headers,
                    "x-mlflow-experiment-id": experiment_id,
                },
                timeout=30,
            )
            resp.raise_for_status()
            ok += 1
        except requests.RequestException as e:
            detail = ""
            if hasattr(e, "response") and e.response is not None:
                detail = f" ({e.response.status_code})"
            print(f"Trace push failed{detail}: {e}", file=sys.stderr)
            err += 1

    return ok, err
