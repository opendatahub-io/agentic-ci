# Lifecycle Outcome Events

`agentic_ci.lifecycle` provides a small, versioned contract for reporting
outcomes from Autofix and other SDLC automation. The contract contains no
storage or transport implementation. Producers can serialize an event and send
it to the system used by their pipeline.

## Event shape

`OutcomeEvent` is immutable after construction. It contains:

- `schema_version` and `event_type` for contract negotiation.
- A deterministic `event_id`, derived from the schema major version,
  `correlation_id`, `stage`, and `attempt`.
- `stage` values for `eligibility`, `analyzed`, `proposed`, `iterated`,
  `blocked`, `rejected`, `abandoned`, `stale`, `merged`, and `reconciliation`.
- `final_outcome`, including `reconciled` for reconciliation events.
- Event and stage timestamps, attempt number, verdict, reason, harness, model,
  and optional token, cache-token, cost, and latency metrics.
- Optional correlation fields for Jira key/project, repository/branch, forge
  URL/ID, pipeline/job, and trace/session identifiers.

Jira fields are optional. A non-Jira SDLC pipeline can use the same contract
with only its repository, forge, pipeline, or telemetry identifiers.

## Create and serialize an event

```python
from agentic_ci.lifecycle import (
    OutcomeCorrelation,
    OutcomeEvent,
    OutcomeMetrics,
)

event = OutcomeEvent.create(
    correlation_id="AIPCC-31384",
    stage="proposed",
    attempt=1,
    correlation=OutcomeCorrelation(
        jira_key="AIPCC-31384",
        jira_project="AIPCC",
        repository="https://github.com/example/project",
        branch="codex/aipcc-31384",
        forge_url="https://github.com/example/project/pull/42",
        forge_id="42",
        pipeline_id="pipeline-7",
        job_id="job-12",
        trace_id="trace-abc",
        session_id="session-xyz",
    ),
    harness="codex",
    model="gpt-5.6-luna",
    verdict="committed",
    reason="Merge request created",
    metrics=OutcomeMetrics(
        input_tokens=1200,
        output_tokens=300,
        cache_read_tokens=800,
        cache_write_tokens=100,
        total_tokens=2400,
        cost_usd=0.03,
        latency_ms=4200,
    ),
)

payload = event.to_dict()
json_line = event.to_json()
same_id = OutcomeEvent.create(correlation_id="AIPCC-31384", stage="proposed", attempt=1).event_id
assert same_id == event.event_id
```

Use a new `attempt` for a new execution of the same lifecycle stage. Retries
that publish the same stage attempt must reuse the same `correlation_id`, stage,
and attempt so downstream consumers can de-duplicate by `event_id`.

## Compatibility

`validate_outcome_event(payload)` accepts schema version `1.x`. Unknown fields
are retained in `OutcomeEvent.extensions`, which allows minor-version additions
without breaking existing consumers. A major version change raises
`LifecycleEventError`. The checked-in JSON Schema is available through
`outcome_event_schema()` and at
`src/agentic_ci/data/outcome_event.schema.json`.

::: agentic_ci.lifecycle
