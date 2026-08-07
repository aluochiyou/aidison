from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from aidison.api.app import _project_event_payload
from aidison.infrastructure.orm import DomainEventRow


def test_sse_event_payload_preserves_server_cursor_and_time() -> None:
    project_id = uuid4()
    created_at = datetime(2026, 8, 7, 2, 3, tzinfo=UTC)
    row = DomainEventRow(
        id=uuid4(),
        project_id=project_id,
        project_seq=14,
        event_type="job.completed",
        payload={"job_id": "job-1"},
        created_at=created_at,
    )

    payload = _project_event_payload(project_id, row, encode_datetime=True)

    assert payload == {
        "id": f"{project_id}:14",
        "sequence": 14,
        "type": "job.completed",
        "payload": {"job_id": "job-1"},
        "created_at": "2026-08-07T02:03:00+00:00",
    }
