from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from aidison.api.app import _project_event_payload
from aidison.infrastructure.orm import DomainEventRow


def test_sse_event_payload_preserves_server_cursor_and_time() -> None:
    project_id = uuid4()
    event_id = uuid4()
    aggregate_id = uuid4()
    correlation_id = uuid4()
    causation_id = uuid4()
    created_at = datetime(2026, 8, 7, 2, 3, tzinfo=UTC)
    row = DomainEventRow(
        id=14,
        project_id=project_id,
        project_seq=14,
        event_type="agent_run.completed",
        payload={"agent_run_id": "run-1"},
        event_id=event_id,
        schema_version=1,
        aggregate_type="agent_run",
        aggregate_id=aggregate_id,
        aggregate_version=3,
        occurred_at=created_at,
        payload_hash="a" * 64,
        correlation_id=correlation_id,
        causation_id=causation_id,
        actor="application_service",
        source_component="project_application",
        artifact_refs=["artifact://plan-input"],
        created_at=created_at,
    )

    payload = _project_event_payload(project_id, row, encode_datetime=True)

    assert payload == {
        "schema_version": "project-event.v1",
        "id": f"{project_id}:14",
        "sequence": 14,
        "type": "agent_run.completed",
        "payload": {"agent_run_id": "run-1"},
        "event_id": str(event_id),
        "event_schema_version": 1,
        "aggregate_type": "agent_run",
        "aggregate_id": str(aggregate_id),
        "aggregate_version": 3,
        "occurred_at": "2026-08-07T02:03:00+00:00",
        "payload_hash": "a" * 64,
        "correlation_id": str(correlation_id),
        "causation_id": str(causation_id),
        "actor": "application_service",
        "source_component": "project_application",
        "artifact_refs": ["artifact://plan-input"],
        "created_at": "2026-08-07T02:03:00+00:00",
    }
