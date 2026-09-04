"""Generic event transport for the existing OTLP trace pipeline.

This module owns transport and serialization only. Producers own event schemas,
workflow timing, outcome classification, and correlation-field meaning.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeAlias
from urllib.parse import urlsplit

import requests

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
Correlation: TypeAlias = Mapping[str, str | None]

MAX_EVENT_BYTES = 65_536
MAX_STRING_LENGTH = 16_384
MAX_NESTING_DEPTH = 20
MAX_COLLECTION_ITEMS = 1_000
_LOG_FILENAME = "claude-otel.jsonl"
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "body",
        "client_secret",
        "comment",
        "content",
        "cookie",
        "description",
        "diff",
        "email",
        "email_address",
        "password",
        "passwd",
        "patch",
        "private_key",
        "prompt",
        "raw_request",
        "raw_response",
        "refresh_token",
        "request_body",
        "response_body",
        "review_text",
        "secret",
        "set_cookie",
        "source_code",
        "user_email",
        "username",
    }
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bbearer\s+[A-Z0-9._~+/=-]{8,}", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_URL_USERINFO_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/?#\s]*@", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"[?&](?:access_token|api_key|apikey|authorization|password|secret|token)=[^&#\s]+",
    re.IGNORECASE,
)


class TelemetryEvent(Protocol):
    """Protocol accepted by :func:`emit_event`.

    Producers can pass any immutable event object that returns a JSON-like
    mapping from ``to_dict()``. Plain mappings are accepted as well.
    """

    def to_dict(self) -> Mapping[str, JSONValue]:
        """Return the event's JSON-compatible fields."""


class TelemetryEventError(ValueError):
    """Raised when a producer event cannot be transported safely."""


class TelemetryExportError(RuntimeError):
    """Raised when an OTLP event export request fails."""


def _normalise_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _is_sensitive_key(value: str) -> bool:
    normalised = _normalise_key(value)
    return normalised in _SENSITIVE_KEYS or normalised.endswith(
        (
            "_access_token",
            "_api_key",
            "_authorization",
            "_password",
            "_private_key",
            "_secret",
        )
    )


def _validate_string(value: str, path: str) -> None:
    if len(value) > MAX_STRING_LENGTH:
        raise TelemetryEventError(f"{path} exceeds the maximum string length")
    if (
        _EMAIL_RE.search(value)
        or _BEARER_RE.search(value)
        or _PRIVATE_KEY_RE.search(value)
        or _URL_USERINFO_RE.search(value)
        or _SECRET_QUERY_RE.search(value)
    ):
        raise TelemetryEventError(f"{path} contains sensitive content")


def _validate_json(value: Any, path: str = "event", depth: int = 0) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise TelemetryEventError(f"{path} exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        _validate_string(value, path)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TelemetryEventError(f"{path} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise TelemetryEventError(f"{path} contains too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise TelemetryEventError(f"{path} keys must be non-empty strings")
            if _is_sensitive_key(key):
                raise TelemetryEventError(f"{path}.{key} is not allowed in telemetry events")
            _validate_json(item, f"{path}.{key}", depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise TelemetryEventError(f"{path} contains too many items")
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]", depth + 1)
        return
    raise TelemetryEventError(f"{path} contains unsupported value {type(value).__name__}")


def validate_event(event: Mapping[str, JSONValue] | TelemetryEvent) -> dict[str, JSONValue]:
    """Validate and copy a generic event before transport.

    Transport requires a non-empty ``event_type`` field. Other fields remain
    producer-owned and are preserved unchanged after privacy and size checks.
    """
    if isinstance(event, Mapping):
        payload = dict(event)
    else:
        to_dict = getattr(event, "to_dict", None)
        if not callable(to_dict):
            raise TelemetryEventError("event must be a mapping or provide to_dict()")
        raw_payload = to_dict()
        if not isinstance(raw_payload, Mapping):
            raise TelemetryEventError("to_dict() must return a mapping")
        payload = dict(raw_payload)
    _validate_json(payload)
    event_type = payload.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise TelemetryEventError("event_type must be a non-empty string")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise TelemetryEventError("event exceeds the maximum serialized size")
    return payload


def _validate_correlation(correlation: Correlation | None) -> dict[str, str | None]:
    if correlation is None:
        return {}
    if not isinstance(correlation, Mapping):
        raise TelemetryEventError("correlation must be a mapping")
    result: dict[str, str | None] = {}
    for key, value in correlation.items():
        if not isinstance(key, str) or not key:
            raise TelemetryEventError("correlation keys must be non-empty strings")
        if _is_sensitive_key(key):
            raise TelemetryEventError(f"correlation.{key} is not allowed in telemetry events")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise TelemetryEventError(f"correlation.{key} must be a non-empty string or null")
        if value is not None:
            _validate_string(value, f"correlation.{key}")
        result[key] = value
    return result


def _valid_id(value: str | None, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _attribute(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _trace_id(event: Mapping[str, JSONValue], correlation: Mapping[str, str | None]) -> str:
    supplied = correlation.get("trace_id")
    if _valid_id(supplied, 32):
        return str(supplied)
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return uuid.uuid5(uuid.NAMESPACE_URL, event_id).hex
    return uuid.uuid4().hex


def build_event_record(
    event: Mapping[str, JSONValue] | TelemetryEvent,
    *,
    correlation: Correlation | None = None,
    parent_span_id: str | None = None,
) -> dict[str, Any]:
    """Build an OTLP trace record for one generic event.

    The record uses a zero-duration span so existing JSONL and MLflow trace
    exporters consume events without a separate storage or UI integration.
    Correlation values are emitted as namespaced attributes and never
    interpreted by this module.
    """
    payload = validate_event(event)
    correlation_values = _validate_correlation(correlation)
    trace_id = _trace_id(payload, correlation_values)
    span_id = uuid.uuid4().hex[:16]
    timestamp_ns = time.time_ns()
    attributes = [
        _attribute("event.name", str(payload["event_type"])),
    ]
    event_id = payload.get("event_id")
    if isinstance(event_id, str) and event_id:
        attributes.append(_attribute("event.id", event_id))
    for key, value in correlation_values.items():
        if value is not None:
            attributes.append(_attribute(f"telemetry.correlation.{key}", value))

    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": str(payload["event_type"]),
        "kind": 1,
        "startTimeUnixNano": str(timestamp_ns),
        "endTimeUnixNano": str(timestamp_ns),
        "status": {"code": 0},
        "attributes": attributes,
        "events": [
            {
                "name": str(payload["event_type"]),
                "timeUnixNano": str(timestamp_ns),
                "attributes": [_attribute("telemetry.event", json.dumps(payload, sort_keys=True))],
            }
        ],
    }
    if _valid_id(parent_span_id, 16):
        span["parentSpanId"] = parent_span_id

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "path": "/v1/traces",
        "payload": {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attribute("service.name", "agentic-ci")]},
                    "scopeSpans": [
                        {
                            "scope": {"name": "agentic-ci.telemetry"},
                            "spans": [span],
                        }
                    ],
                }
            ]
        },
    }


def _resolve_log_root(log_root: str | Path) -> Path:
    root = Path(log_root)
    if root.is_symlink():
        raise TelemetryEventError("log_root must not be a symbolic link")
    root.mkdir(parents=True, exist_ok=True)
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise TelemetryEventError("log_root must be a valid directory") from exc
    if not resolved.is_dir():
        raise TelemetryEventError("log_root must be a directory")
    return resolved


def _open_log(root: Path, flags: int, mode: str):
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            file_fd = os.open(
                _LOG_FILENAME,
                flags | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise TelemetryEventError("telemetry log must be a regular file in log_root") from exc
    finally:
        os.close(directory_fd)
    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
        os.close(file_fd)
        raise TelemetryEventError("telemetry log must be a regular file in log_root")
    return os.fdopen(file_fd, mode, encoding="utf-8")


def _find_parent_span_id(log_root: Path, trace_id: str) -> str | None:
    try:
        with _open_log(log_root, os.O_RDONLY, "r") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "/v1/traces" not in record.get("path", ""):
                    continue
                for resource_spans in (record.get("payload") or {}).get("resourceSpans", []):
                    for scope_spans in resource_spans.get("scopeSpans", []):
                        for span in scope_spans.get("spans", []):
                            if span.get("traceId") == trace_id and not span.get("parentSpanId"):
                                parent = span.get("spanId")
                                if _valid_id(parent, 16):
                                    return parent
    except FileNotFoundError:
        return None
    return None


def append_event(
    log_root: str | Path,
    event: Mapping[str, JSONValue] | TelemetryEvent,
    *,
    correlation: Correlation | None = None,
    parent_span_id: str | None = None,
) -> dict[str, Any]:
    """Append an event to the fixed OTLP JSONL file beneath trusted ``log_root``."""
    root = _resolve_log_root(log_root)
    record = build_event_record(event, correlation=correlation, parent_span_id=parent_span_id)
    span = record["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    if "parentSpanId" not in span:
        parent = _find_parent_span_id(root, span["traceId"])
        if parent:
            span["parentSpanId"] = parent
    with _open_log(root, os.O_WRONLY | os.O_CREAT | os.O_APPEND, "a") as stream:
        stream.write(json.dumps(record) + "\n")
    return record


def _post_record(
    endpoint: str,
    record: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None,
    timeout: float,
) -> None:
    url = endpoint.rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TelemetryEventError("endpoint must be an HTTP(S) URL without userinfo")
    if not url.endswith("/v1/traces"):
        url = f"{url}/v1/traces"
    try:
        response = requests.post(
            url,
            json=record["payload"],
            headers=dict(headers or {}),
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise TelemetryExportError(f"telemetry event export failed: {exc}") from exc


def export_event(
    endpoint: str,
    event: Mapping[str, JSONValue] | TelemetryEvent,
    *,
    correlation: Correlation | None = None,
    parent_span_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """Export one generic event to an OTLP HTTP trace endpoint."""
    record = build_event_record(event, correlation=correlation, parent_span_id=parent_span_id)
    _post_record(endpoint, record, headers=headers, timeout=timeout)
    return record


def emit_event(
    event: Mapping[str, JSONValue] | TelemetryEvent,
    *,
    log_root: str | Path | None = None,
    endpoint: str | None = None,
    correlation: Correlation | None = None,
    parent_span_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """Emit one event to JSONL, OTLP HTTP, or both.

    At least one destination is required. This function does not catch write
    or export errors, allowing callers to preserve their workflow's result
    while deciding how to report transport failure.
    """
    if log_root is None and endpoint is None:
        raise TelemetryEventError("log_root or endpoint is required")
    if endpoint is not None and not endpoint.strip():
        raise TelemetryEventError("endpoint must be a non-empty string")
    if log_root is not None:
        record = append_event(
            log_root,
            event,
            correlation=correlation,
            parent_span_id=parent_span_id,
        )
        if endpoint is not None:
            _post_record(
                endpoint,
                record,
                headers=headers,
                timeout=timeout,
            )
        return record
    return export_event(
        endpoint or "",
        event,
        correlation=correlation,
        parent_span_id=parent_span_id,
        headers=headers,
        timeout=timeout,
    )


__all__ = [
    "Correlation",
    "JSONValue",
    "TelemetryEvent",
    "TelemetryEventError",
    "TelemetryExportError",
    "append_event",
    "build_event_record",
    "emit_event",
    "export_event",
    "validate_event",
]
