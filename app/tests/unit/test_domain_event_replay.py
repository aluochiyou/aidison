from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from aidison.domain.events import (
    DomainEventMetadata,
    EventIntegrityError,
    EventSequenceGap,
    EventUpcasterRegistry,
    ExecutionPlanReplaySnapshot,
    ExecutionPlanReplayState,
    ReplaySnapshotIntegrityError,
    StoredDomainEvent,
    UnsupportedEventSchema,
    canonical_payload_hash,
    execution_plan_replay_state_hash,
    replay_execution_plan,
    replay_execution_plan_from_snapshot,
)
from aidison.domain.models import ExecutionPlanProposal, ExecutionPlanStatus

PROJECT_ID = UUID("10000000-0000-4000-8000-000000000001")
PLAN_ID = UUID("20000000-0000-4000-8000-000000000002")
PROPOSED_EVENT_ID = UUID("30000000-0000-4000-8000-000000000003")
RESOLVED_EVENT_ID = UUID("40000000-0000-4000-8000-000000000004")
RECORDED_AT = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
RESOLVED_AT = datetime(2026, 10, 6, 8, 5, tzinfo=UTC)


def _plan_payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": PLAN_ID,
        "project_id": PROJECT_ID,
        "basis_hash": "a" * 64,
        "objective": "核验供电、控制与预算约束",
        "work_summary": ("检索候选", "独立核验兼容性"),
        "allowed_coordination_modes": ("decompose", "verify"),
        "max_concurrency": 2,
        "max_token_budget": 12_000,
        "max_duration_seconds": 3_600,
        "research_depth": "standard",
        "allowed_tool_classes": ("web_search", "github_read"),
        "allowed_effects": ("read",),
        "requires_independent_verification": True,
        "requires_result_approval": True,
        "status": ExecutionPlanStatus.PROPOSED,
        "created_at": RECORDED_AT,
    }
    values.update(updates)
    return ExecutionPlanProposal(**values).model_dump(mode="json")


def _stored_event(
    *,
    event_id: UUID,
    event_type: str,
    schema_version: int,
    aggregate_version: int,
    project_seq: int,
    payload: dict[str, object],
    payload_hash: str | None = None,
) -> StoredDomainEvent:
    metadata = DomainEventMetadata(
        event_id=event_id,
        schema_version=schema_version,
        aggregate_type="execution_plan",
        aggregate_id=PLAN_ID,
        aggregate_version=aggregate_version,
        occurred_at=RECORDED_AT,
        payload_hash=payload_hash or canonical_payload_hash(payload),
        correlation_id=PLAN_ID,
        causation_id=PROPOSED_EVENT_ID if aggregate_version > 1 else None,
        actor="application_service",
        source_component="project_application",
    )
    return StoredDomainEvent(
        **metadata.model_dump(),
        project_id=PROJECT_ID,
        project_seq=project_seq,
        event_type=event_type,
        payload=payload,
        recorded_at=RECORDED_AT,
    )


def _proposed_event(
    *,
    event_id: UUID = PROPOSED_EVENT_ID,
    aggregate_version: int = 1,
    project_seq: int = 1,
    plan: dict[str, object] | None = None,
    schema_version: int = 1,
    payload_hash: str | None = None,
) -> StoredDomainEvent:
    payload = {"plan": plan or _plan_payload()}
    return _stored_event(
        event_id=event_id,
        event_type="execution_plan.proposed",
        schema_version=schema_version,
        aggregate_version=aggregate_version,
        project_seq=project_seq,
        payload=payload,
        payload_hash=payload_hash,
    )


def _resolved_event(
    *,
    event_id: UUID = RESOLVED_EVENT_ID,
    aggregate_version: int = 2,
    project_seq: int = 2,
) -> StoredDomainEvent:
    plan = ExecutionPlanProposal.model_validate(_plan_payload())
    return _stored_event(
        event_id=event_id,
        event_type="execution_plan.resolved",
        schema_version=1,
        aggregate_version=aggregate_version,
        project_seq=project_seq,
        payload={
            "execution_plan_id": str(PLAN_ID),
            "scope_hash": plan.scope_hash,
            "status": ExecutionPlanStatus.APPROVED.value,
            "resolved_at": RESOLVED_AT.isoformat(),
        },
    )


def test_replay_rebuilds_plan_from_proposed_v1_and_resolved_v2() -> None:
    state = replay_execution_plan([_proposed_event(), _resolved_event()])

    assert isinstance(state, ExecutionPlanReplayState)
    assert state.aggregate_id == PLAN_ID
    assert state.aggregate_version == 2
    assert state.proposal.id == PLAN_ID
    assert state.proposal.project_id == PROJECT_ID
    assert state.proposal.status is ExecutionPlanStatus.APPROVED
    assert state.proposal.resolved_at == RESOLVED_AT


def test_replay_is_idempotent_for_duplicate_event_id() -> None:
    proposed = _proposed_event()

    once = replay_execution_plan([proposed])
    duplicated = replay_execution_plan([proposed, proposed])

    assert duplicated == once
    assert duplicated.aggregate_version == 1


def test_snapshot_tail_replay_matches_from_zero_replay() -> None:
    proposed = _proposed_event()
    resolved = _resolved_event()
    snapshot = ExecutionPlanReplaySnapshot.from_state(
        replay_execution_plan([proposed]),
        project_seq=proposed.project_seq,
    )

    from_zero = replay_execution_plan([proposed, resolved])
    from_snapshot = replay_execution_plan_from_snapshot(snapshot, [resolved])

    assert from_snapshot == from_zero
    assert execution_plan_replay_state_hash(from_snapshot) == execution_plan_replay_state_hash(
        from_zero
    )


def test_snapshot_replay_rejects_an_event_before_its_cursor() -> None:
    proposed = _proposed_event()
    snapshot = ExecutionPlanReplaySnapshot.from_state(
        replay_execution_plan([proposed]),
        project_seq=proposed.project_seq,
    )

    with pytest.raises(ReplaySnapshotIntegrityError, match="at or before its cursor"):
        replay_execution_plan_from_snapshot(snapshot, [proposed])


def test_snapshot_rejects_tampered_state_hash() -> None:
    snapshot = ExecutionPlanReplaySnapshot.from_state(
        replay_execution_plan([_proposed_event()]),
        project_seq=1,
    )

    with pytest.raises(ValueError, match="state hash"):
        ExecutionPlanReplaySnapshot(
            **(snapshot.model_dump() | {"state_hash": "0" * 64})
        )


def test_replay_rejects_duplicate_event_id_with_different_content() -> None:
    proposed = _proposed_event()
    changed = _proposed_event(
        event_id=PROPOSED_EVENT_ID,
        plan=_plan_payload(objective="tampered but self-consistent payload"),
    )

    with pytest.raises(EventIntegrityError, match="duplicate event ID"):
        replay_execution_plan([proposed, changed])


def test_replay_rejects_different_events_at_same_aggregate_version() -> None:
    conflicting = _proposed_event(
        event_id=UUID("50000000-0000-4000-8000-000000000005"),
        plan=_plan_payload(objective="另一个计划"),
        project_seq=2,
    )

    with pytest.raises(EventIntegrityError, match="aggregate version|conflict"):
        replay_execution_plan([_proposed_event(), conflicting])


def test_replay_rejects_aggregate_version_gap() -> None:
    gap = _proposed_event(
        event_id=UUID("60000000-0000-4000-8000-000000000006"),
        aggregate_version=3,
        project_seq=2,
    )

    with pytest.raises(EventSequenceGap):
        replay_execution_plan([_proposed_event(), gap])


def test_replay_rejects_unknown_schema_version() -> None:
    with pytest.raises(UnsupportedEventSchema):
        replay_execution_plan([_proposed_event(schema_version=999)])


def test_replay_rejects_payload_hash_mismatch() -> None:
    with pytest.raises(EventIntegrityError, match="hash"):
        replay_execution_plan([_proposed_event(payload_hash="0" * 64)])


def test_upcaster_registry_applies_adjacent_steps_without_mutating_stored_event() -> None:
    registry = EventUpcasterRegistry()

    def v1_to_v2(payload: dict[str, object]) -> dict[str, object]:
        payload["strategy"] = "bounded"
        return payload

    def v2_to_v3(payload: dict[str, object]) -> dict[str, object]:
        return {**payload, "budget_policy": "hard_limit"}

    registry.register(
        event_type="execution_plan.proposed",
        from_version=1,
        upcaster=v1_to_v2,
    )
    registry.register(
        event_type="execution_plan.proposed",
        from_version=2,
        upcaster=v2_to_v3,
    )
    stored = _proposed_event()

    upgraded = registry.upcast(stored, target_version=3)

    assert stored.schema_version == 1
    assert "strategy" not in stored.payload
    assert upgraded.schema_version == 3
    assert upgraded.payload["strategy"] == "bounded"
    assert upgraded.payload["budget_policy"] == "hard_limit"
    assert upgraded.payload_hash == canonical_payload_hash(upgraded.payload)
    assert upgraded.event_id == stored.event_id
    assert upgraded.aggregate_version == stored.aggregate_version


def test_upcaster_registry_fails_closed_when_an_intermediate_step_is_missing() -> None:
    registry = EventUpcasterRegistry()
    registry.register(
        event_type="execution_plan.proposed",
        from_version=2,
        upcaster=lambda payload: payload,
    )

    with pytest.raises(UnsupportedEventSchema, match="schema 1"):
        registry.upcast(_proposed_event(), target_version=3)


def test_upcaster_registry_rejects_duplicate_step_registration() -> None:
    registry = EventUpcasterRegistry()
    registry.register(
        event_type="execution_plan.proposed",
        from_version=1,
        upcaster=lambda payload: payload,
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            event_type="execution_plan.proposed",
            from_version=1,
            upcaster=lambda payload: payload,
        )


def test_upcaster_registry_validates_stored_hash_before_transforming() -> None:
    registry = EventUpcasterRegistry()
    registry.register(
        event_type="execution_plan.proposed",
        from_version=1,
        upcaster=lambda payload: payload,
    )

    with pytest.raises(EventIntegrityError, match="before upcast"):
        registry.upcast(
            _proposed_event(payload_hash="0" * 64),
            target_version=2,
        )


def test_upcaster_registry_rejects_downcast() -> None:
    registry = EventUpcasterRegistry()
    stored = _proposed_event().model_copy(update={"schema_version": 2})

    with pytest.raises(UnsupportedEventSchema, match="downcast"):
        registry.upcast(stored, target_version=1)
