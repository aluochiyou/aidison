from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from aidison.domain.events import EventIntegrityError, StoredDomainEvent
from aidison.domain.models import RequirementRevision, RequirementStatus
from aidison.domain.requirement_revision_events import (
    RequirementRevisionEventType,
    replay_requirement_revision,
    requirement_revision_event_metadata,
    requirement_revision_replay_state_hash,
)

PROJECT_ID = UUID("10000000-0000-4000-8000-000000000011")
REQUIREMENT_ID = UUID("20000000-0000-4000-8000-000000000012")
APPROVED_AT = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


def _requirement(**updates: object) -> RequirementRevision:
    values: dict[str, object] = {
        "id": REQUIREMENT_ID,
        "project_id": PROJECT_ID,
        "revision": 1,
        "status": RequirementStatus.APPROVED,
        "goal": "在预算内完成可验证的无人机动力系统选型",
        "hard_constraints": ("6S 电池", "持续电流不低于 35A"),
        "preferences": ("优先保留国产替代方案",),
        "approved_at": APPROVED_AT,
        "created_at": APPROVED_AT,
    }
    values.update(updates)
    return RequirementRevision(**values)


def _event(
    *,
    requirement: RequirementRevision | None = None,
    aggregate_version: int = 1,
    event_type: RequirementRevisionEventType = RequirementRevisionEventType.APPROVED,
    payload_override: dict[str, object] | None = None,
) -> StoredDomainEvent:
    value = requirement or _requirement()
    payload = payload_override or {"requirement": value.model_dump(mode="json"), "module_count": 2}
    metadata = requirement_revision_event_metadata(requirement=value, payload=payload)
    return StoredDomainEvent(
        **(
            metadata.model_dump()
            | {
                "aggregate_version": aggregate_version,
                "payload_hash": metadata.payload_hash,
            }
        ),
        project_id=value.project_id,
        project_seq=1,
        event_type=event_type.value,
        payload=payload,
        recorded_at=APPROVED_AT,
    )


def test_requirement_revision_replay_restores_the_approved_immutable_value() -> None:
    state = replay_requirement_revision([_event()])

    assert state.value == _requirement()
    assert state.aggregate_version == 1
    assert len(requirement_revision_replay_state_hash(state)) == 64


def test_requirement_revision_replay_allows_only_an_identical_retry() -> None:
    event = _event()

    assert replay_requirement_revision([event, event]) == replay_requirement_revision([event])


def test_requirement_revision_replay_rejects_second_or_tampered_event() -> None:
    with pytest.raises(EventIntegrityError, match="duplicate"):
        replay_requirement_revision([_event(), _event(requirement=_requirement(revision=2))])

    tampered = _event().model_copy(
        update={
            "payload": {
                "requirement": _requirement(goal="篡改后的目标").model_dump(mode="json"),
                "module_count": 2,
            }
        }
    )
    with pytest.raises(EventIntegrityError, match="hash"):
        replay_requirement_revision([tampered])
