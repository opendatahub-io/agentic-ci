"""Versioned lifecycle outcome events for SDLC automation.

This module defines a small, immutable event contract that SDLC pipelines can
use to publish lifecycle outcomes without coupling producers to storage or a
particular issue tracker.  Jira fields are optional correlation attributes,
not workflow requirements.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast

SCHEMA_VERSION = "1.0"
EVENT_TYPE = "sdlc.lifecycle.outcome"

LifecycleStage: TypeAlias = Literal[
    "eligibility",
    "analyzed",
    "proposed",
    "iterated",
    "blocked",
    "rejected",
    "abandoned",
    "stale",
    "merged",
    "reconciliation",
]
FinalOutcome: TypeAlias = Literal[
    "eligible",
    "analyzed",
    "proposed",
    "iterated",
    "blocked",
    "rejected",
    "abandoned",
    "stale",
    "merged",
    "reconciled",
]
JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

VALID_STAGES = frozenset(
    {
        "eligibility",
        "analyzed",
        "proposed",
        "iterated",
        "blocked",
        "rejected",
        "abandoned",
        "stale",
        "merged",
        "reconciliation",
    }
)
VALID_OUTCOMES = frozenset(
    {
        "eligible",
        "analyzed",
        "proposed",
        "iterated",
        "blocked",
        "rejected",
        "abandoned",
        "stale",
        "merged",
        "reconciled",
    }
)
_DEFAULT_OUTCOME_BY_STAGE: dict[str, str] = {
    "eligibility": "eligible",
    "analyzed": "analyzed",
    "proposed": "proposed",
    "iterated": "iterated",
    "blocked": "blocked",
    "rejected": "rejected",
    "abandoned": "abandoned",
    "stale": "stale",
    "merged": "merged",
    "reconciliation": "reconciled",
}
_SCHEMA_VERSION_RE = re.compile(r"^(?P<major>[1-9][0-9]*)\.(?P<minor>[0-9]+)$")
_EVENT_ID_RE = re.compile(r"^outcome-v[1-9][0-9]*-[0-9a-f]{64}$")
_KNOWN_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_type",
        "event_id",
        "correlation_id",
        "stage",
        "final_outcome",
        "event_timestamp",
        "stage_timestamp",
        "attempt",
        "correlation",
        "harness",
        "model",
        "verdict",
        "reason",
        "metrics",
        "metadata",
    }
)


class LifecycleEventError(ValueError):
    """Raised when a lifecycle event is malformed or incompatible."""


def is_compatible_schema_version(version: str) -> bool:
    """Return whether *version* can be read by this contract.

    Minor versions are forward-compatible because unknown fields are retained
    in :attr:`OutcomeEvent.extensions`.  A major version change requires a
    new reader and is rejected.
    """

    match = _SCHEMA_VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    return match is not None and int(match.group("major")) == 1


def _check_schema_version(version: str) -> None:
    if not is_compatible_schema_version(version):
        raise LifecycleEventError(
            f"Unsupported lifecycle event schema version {version!r}; supported major version is 1"
        )


def _check_optional_string(name: str, value: Any) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise LifecycleEventError(f"{name} must be a non-empty string or null")


def _freeze_json(value: Any, *, path: str = "value") -> Any:
    """Recursively freeze JSON-compatible values for immutable event fields."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LifecycleEventError(f"{path} must contain finite numbers")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise LifecycleEventError(f"{path} keys must be strings")
            frozen[key] = _freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, path=f"{path}[]") for item in value)
    raise LifecycleEventError(f"{path} must contain JSON-compatible values")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_timestamp(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise LifecycleEventError(f"{name} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleEventError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise LifecycleEventError(f"{name} must include a timezone")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event_id_for(
    correlation_id: str,
    stage: str,
    attempt: int = 1,
    *,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    """Return deterministic, idempotent ID for one lifecycle stage attempt.

    Producers must reuse ``correlation_id``, ``stage``, and ``attempt`` when
    retrying publication of the same logical event.  Event payload changes do
    not create a second ID for that stage attempt.
    """

    _check_schema_version(schema_version)
    _check_optional_string("correlation_id", correlation_id)
    _check_optional_string("stage", stage)
    if stage not in VALID_STAGES:
        raise LifecycleEventError(f"unknown lifecycle stage: {stage!r}")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise LifecycleEventError("attempt must be a positive integer")

    major = schema_version.split(".", maxsplit=1)[0]
    identity = json.dumps(
        [EVENT_TYPE, major, correlation_id.strip(), stage, attempt],
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return f"outcome-v{major}-{digest}"


@dataclass(frozen=True, slots=True)
class OutcomeCorrelation:
    """Optional identifiers that connect an event to SDLC systems."""

    jira_key: str | None = None
    jira_project: str | None = None
    repository: str | None = None
    branch: str | None = None
    forge_url: str | None = None
    forge_id: str | None = None
    pipeline_id: str | None = None
    job_id: str | None = None
    trace_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _check_optional_string(name, getattr(self, name))

    def to_dict(self) -> dict[str, str | None]:
        """Return JSON-ready correlation fields."""

        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "OutcomeCorrelation":
        """Build correlation fields from a JSON object."""

        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise LifecycleEventError("correlation must be an object")
        fields = {name: value.get(name) for name in cls.__dataclass_fields__ if name in value}
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class OutcomeMetrics:
    """Optional token, cost, and latency measurements for one event."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise LifecycleEventError(f"{name} must be a non-negative integer or null")
        for name in ("cost_usd", "latency_ms"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise LifecycleEventError(f"{name} must be a non-negative number or null")

    def to_dict(self) -> dict[str, int | float | None]:
        """Return JSON-ready metrics fields."""

        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "OutcomeMetrics":
        """Build metrics from a JSON object."""

        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise LifecycleEventError("metrics must be an object")
        fields = {name: value.get(name) for name in cls.__dataclass_fields__ if name in value}
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class OutcomeEvent:
    """Immutable, versioned outcome event shared by SDLC pipelines."""

    schema_version: str
    event_type: str
    event_id: str
    correlation_id: str
    stage: LifecycleStage
    final_outcome: FinalOutcome
    event_timestamp: str
    stage_timestamp: str
    attempt: int
    correlation: OutcomeCorrelation = field(default_factory=OutcomeCorrelation)
    harness: str | None = None
    model: str | None = None
    verdict: str | None = None
    reason: str | None = None
    metrics: OutcomeMetrics = field(default_factory=OutcomeMetrics)
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    extensions: Mapping[str, JSONValue] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _check_schema_version(self.schema_version)
        if self.event_type != EVENT_TYPE:
            raise LifecycleEventError(f"event_type must be {EVENT_TYPE!r}")
        _check_optional_string("correlation_id", self.correlation_id)
        if self.stage not in VALID_STAGES:
            raise LifecycleEventError(f"unknown lifecycle stage: {self.stage!r}")
        if self.final_outcome not in VALID_OUTCOMES:
            raise LifecycleEventError(f"unknown final outcome: {self.final_outcome!r}")
        expected_id = event_id_for(
            self.correlation_id,
            self.stage,
            self.attempt,
            schema_version=self.schema_version,
        )
        if not _EVENT_ID_RE.fullmatch(self.event_id) or self.event_id != expected_id:
            raise LifecycleEventError("event_id does not match deterministic event identity")
        _validate_timestamp("event_timestamp", self.event_timestamp)
        _validate_timestamp("stage_timestamp", self.stage_timestamp)
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise LifecycleEventError("attempt must be a positive integer")
        if not isinstance(self.correlation, OutcomeCorrelation):
            raise LifecycleEventError("correlation must be OutcomeCorrelation")
        if not isinstance(self.metrics, OutcomeMetrics):
            raise LifecycleEventError("metrics must be OutcomeMetrics")
        for name in ("harness", "model", "verdict", "reason"):
            _check_optional_string(name, getattr(self, name))
        frozen_metadata = _freeze_json(self.metadata, path="metadata")
        frozen_extensions = _freeze_json(self.extensions, path="extensions")
        if not isinstance(frozen_metadata, Mapping):
            raise LifecycleEventError("metadata must be an object")
        if not isinstance(frozen_extensions, Mapping):
            raise LifecycleEventError("extensions must be an object")
        if set(frozen_extensions) & _KNOWN_EVENT_FIELDS:
            raise LifecycleEventError("extensions cannot replace contract fields")
        object.__setattr__(self, "metadata", frozen_metadata)
        object.__setattr__(self, "extensions", frozen_extensions)

    @classmethod
    def create(
        cls,
        *,
        correlation_id: str,
        stage: LifecycleStage,
        final_outcome: FinalOutcome | None = None,
        event_timestamp: str | None = None,
        stage_timestamp: str | None = None,
        attempt: int = 1,
        correlation: OutcomeCorrelation | Mapping[str, Any] | None = None,
        harness: str | None = None,
        model: str | None = None,
        verdict: str | None = None,
        reason: str | None = None,
        metrics: OutcomeMetrics | Mapping[str, Any] | None = None,
        metadata: Mapping[str, JSONValue] | None = None,
        schema_version: str = SCHEMA_VERSION,
    ) -> "OutcomeEvent":
        """Create one lifecycle event with deterministic ID and timestamps."""

        _check_schema_version(schema_version)
        if final_outcome is None:
            final_outcome = cast(FinalOutcome, _DEFAULT_OUTCOME_BY_STAGE.get(stage, ""))
        event_time = event_timestamp or _utc_now()
        return cls(
            schema_version=schema_version,
            event_type=EVENT_TYPE,
            event_id=event_id_for(
                correlation_id,
                stage,
                attempt,
                schema_version=schema_version,
            ),
            correlation_id=correlation_id,
            stage=stage,
            final_outcome=final_outcome,
            event_timestamp=event_time,
            stage_timestamp=stage_timestamp or event_time,
            attempt=attempt,
            correlation=(
                correlation
                if isinstance(correlation, OutcomeCorrelation)
                else OutcomeCorrelation.from_dict(correlation)
            ),
            harness=harness,
            model=model,
            verdict=verdict,
            reason=reason,
            metrics=(
                metrics
                if isinstance(metrics, OutcomeMetrics)
                else OutcomeMetrics.from_dict(metrics)
            ),
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready copy of this event."""

        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "stage": self.stage,
            "final_outcome": self.final_outcome,
            "event_timestamp": self.event_timestamp,
            "stage_timestamp": self.stage_timestamp,
            "attempt": self.attempt,
            "correlation": self.correlation.to_dict(),
            "harness": self.harness,
            "model": self.model,
            "verdict": self.verdict,
            "reason": self.reason,
            "metrics": self.metrics.to_dict(),
            "metadata": _thaw_json(self.metadata),
        }
        payload.update(_thaw_json(self.extensions))
        return payload

    def to_json(self) -> str:
        """Serialize this event using stable JSON key ordering."""

        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OutcomeEvent":
        """Parse and validate an event, accepting compatible minor versions."""

        if not isinstance(payload, Mapping):
            raise LifecycleEventError("lifecycle event must be an object")
        missing = (
            _KNOWN_EVENT_FIELDS
            - set(payload)
            - {
                "harness",
                "model",
                "verdict",
                "reason",
            }
        )
        if missing:
            raise LifecycleEventError(f"lifecycle event missing fields: {sorted(missing)}")
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, str):
            raise LifecycleEventError("schema_version must be a string")
        _check_schema_version(schema_version)
        if not isinstance(payload["correlation"], Mapping):
            raise LifecycleEventError("correlation must be an object")
        if not isinstance(payload["metrics"], Mapping):
            raise LifecycleEventError("metrics must be an object")
        extensions = {
            key: value for key, value in payload.items() if key not in _KNOWN_EVENT_FIELDS
        }
        return cls(
            schema_version=schema_version,
            event_type=payload["event_type"],
            event_id=payload["event_id"],
            correlation_id=payload["correlation_id"],
            stage=payload["stage"],
            final_outcome=payload["final_outcome"],
            event_timestamp=payload["event_timestamp"],
            stage_timestamp=payload["stage_timestamp"],
            attempt=payload["attempt"],
            correlation=OutcomeCorrelation.from_dict(payload["correlation"]),
            harness=payload.get("harness"),
            model=payload.get("model"),
            verdict=payload.get("verdict"),
            reason=payload.get("reason"),
            metrics=OutcomeMetrics.from_dict(payload["metrics"]),
            metadata=payload["metadata"],
            extensions=extensions,
        )


def validate_outcome_event(payload: Mapping[str, Any]) -> OutcomeEvent:
    """Validate JSON-like payload and return its immutable event form."""

    return OutcomeEvent.from_dict(payload)


@lru_cache(maxsize=1)
def outcome_event_schema() -> dict[str, Any]:
    """Load the checked-in JSON Schema for lifecycle outcome events."""

    schema_path = files("agentic_ci").joinpath("data/outcome_event.schema.json")
    return json.loads(schema_path.read_text(encoding="utf-8"))


__all__ = [
    "EVENT_TYPE",
    "FinalOutcome",
    "JSONValue",
    "LifecycleEventError",
    "LifecycleStage",
    "OutcomeCorrelation",
    "OutcomeEvent",
    "OutcomeMetrics",
    "SCHEMA_VERSION",
    "event_id_for",
    "is_compatible_schema_version",
    "outcome_event_schema",
    "validate_outcome_event",
]
