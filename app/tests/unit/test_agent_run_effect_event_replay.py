from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from aidison.domain.events import ReplaySnapshotIntegrityError, StoredDomainEvent
from aidison.runtime.agent_run_effect_events import (
    AgentRunEffectEventType,
    AgentRunEffectReplaySnapshot,
    agent_run_effect_event_metadata,
    agent_run_effect_replay_state_hash,
    replay_agent_run_effect,
    replay_agent_run_effect_from_snapshot,
)
from aidison.runtime.agent_run_effects import (
    AgentRunEffect,
    AgentRunEffectIntent,
    AgentRunEffectState,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _stored_event(
    *,
    project_id: UUID,
    effect: AgentRunEffect,
    version: int,
    event_type: AgentRunEffectEventType,
    sequence: int,
) -> StoredDomainEvent:
    payload: dict[str, object] = {"effect": effect.model_dump(mode="json")}
    occurred_at = (
        effect.reconciled_at
        or effect.resolved_at
        or effect.dispatched_at
        or effect.created_at
    )
    metadata = agent_run_effect_event_metadata(
        effect=effect,
        aggregate_version=version,
        payload=payload,
        occurred_at=occurred_at,
        event_type=event_type,
    )
    return StoredDomainEvent(
        **metadata.model_dump(),
        project_id=project_id,
        project_seq=sequence,
        event_type=event_type.value,
        payload=payload,
        recorded_at=occurred_at,
    )


def test_effect_replay_preserves_ambiguous_then_reconciled_lifecycle() -> None:
    project_id = uuid4()
    created_at = datetime(2026, 10, 7, tzinfo=UTC)
    effect = AgentRunEffect(
        intent=AgentRunEffectIntent(
            run_id=uuid4(),
            task_id=uuid4(),
            basis_hash=_hash("basis"),
            approval_ref="effect-approval://fixture",
            effect_kind="project.publish",
            provider="fixture-provider",
            external_idempotency_key="fixture-external-key",
            idempotency_key="fixture-ledger-key",
            request_hash=_hash("request"),
            request_artifact_ref="artifact://effect-request/1",
        ),
        claim_generation=1,
        lease_token=uuid4(),
        state=AgentRunEffectState.PREPARED,
        created_at=created_at,
    )
    dispatched = effect.model_copy(
        update={
            "state": AgentRunEffectState.DISPATCHED,
            "dispatched_at": datetime(2026, 10, 7, 0, 1, tzinfo=UTC),
        }
    )
    ambiguous = dispatched.model_copy(
        update={
            "state": AgentRunEffectState.AMBIGUOUS,
            "normalized_error": "provider_timeout_after_dispatch",
            "resolved_at": datetime(2026, 10, 7, 0, 2, tzinfo=UTC),
        }
    )
    reconciled = ambiguous.model_copy(
        update={
            "state": AgentRunEffectState.SUCCEEDED,
            "reconciliation_artifact_ref": "artifact://effect-reconciliation/1",
            "reconciled_at": datetime(2026, 10, 7, 0, 3, tzinfo=UTC),
        }
    )

    state = replay_agent_run_effect(
        [
            _stored_event(
                project_id=project_id,
                effect=effect,
                version=1,
                event_type=AgentRunEffectEventType.PREPARED,
                sequence=1,
            ),
            _stored_event(
                project_id=project_id,
                effect=dispatched,
                version=2,
                event_type=AgentRunEffectEventType.DISPATCHED,
                sequence=2,
            ),
            _stored_event(
                project_id=project_id,
                effect=ambiguous,
                version=3,
                event_type=AgentRunEffectEventType.AMBIGUOUS,
                sequence=3,
            ),
            _stored_event(
                project_id=project_id,
                effect=reconciled,
                version=4,
                event_type=AgentRunEffectEventType.RECONCILED_SUCCEEDED,
                sequence=4,
            ),
        ]
    )

    assert state.agent_run_effect == reconciled


def test_effect_snapshot_tail_replay_preserves_ambiguous_effect_before_reconciliation() -> None:
    project_id = uuid4()
    created_at = datetime(2026, 10, 7, tzinfo=UTC)
    effect = AgentRunEffect(
        intent=AgentRunEffectIntent(
            run_id=uuid4(),
            task_id=uuid4(),
            basis_hash=_hash("basis"),
            approval_ref="effect-approval://fixture",
            effect_kind="project.publish",
            provider="fixture-provider",
            external_idempotency_key="fixture-external-key",
            idempotency_key="fixture-ledger-key",
            request_hash=_hash("request"),
            request_artifact_ref="artifact://effect-request/1",
        ),
        claim_generation=1,
        lease_token=uuid4(),
        state=AgentRunEffectState.PREPARED,
        created_at=created_at,
    )
    dispatched = effect.model_copy(
        update={
            "state": AgentRunEffectState.DISPATCHED,
            "dispatched_at": datetime(2026, 10, 7, 0, 1, tzinfo=UTC),
        }
    )
    ambiguous = dispatched.model_copy(
        update={
            "state": AgentRunEffectState.AMBIGUOUS,
            "normalized_error": "provider_timeout_after_dispatch",
            "resolved_at": datetime(2026, 10, 7, 0, 2, tzinfo=UTC),
        }
    )
    prepared_event = _stored_event(
        project_id=project_id,
        effect=effect,
        version=1,
        event_type=AgentRunEffectEventType.PREPARED,
        sequence=1,
    )
    dispatched_event = _stored_event(
        project_id=project_id,
        effect=dispatched,
        version=2,
        event_type=AgentRunEffectEventType.DISPATCHED,
        sequence=2,
    )
    ambiguous_event = _stored_event(
        project_id=project_id,
        effect=ambiguous,
        version=3,
        event_type=AgentRunEffectEventType.AMBIGUOUS,
        sequence=3,
    )
    snapshot = AgentRunEffectReplaySnapshot.from_state(
        replay_agent_run_effect([prepared_event]),
        project_seq=prepared_event.project_seq,
        created_at=prepared_event.recorded_at,
    )

    full = replay_agent_run_effect([prepared_event, dispatched_event, ambiguous_event])
    accelerated = replay_agent_run_effect_from_snapshot(
        snapshot,
        [dispatched_event, ambiguous_event],
    )

    assert accelerated == full
    assert accelerated.agent_run_effect.state is AgentRunEffectState.AMBIGUOUS
    assert agent_run_effect_replay_state_hash(accelerated) == agent_run_effect_replay_state_hash(
        full
    )


def test_effect_snapshot_tail_replay_rejects_an_event_at_or_before_its_cursor() -> None:
    project_id = uuid4()
    effect = AgentRunEffect(
        intent=AgentRunEffectIntent(
            run_id=uuid4(),
            task_id=uuid4(),
            basis_hash=_hash("basis"),
            approval_ref="effect-approval://fixture",
            effect_kind="project.publish",
            provider="fixture-provider",
            external_idempotency_key="fixture-external-key",
            idempotency_key="fixture-ledger-key",
            request_hash=_hash("request"),
            request_artifact_ref="artifact://effect-request/1",
        ),
        claim_generation=1,
        lease_token=uuid4(),
        state=AgentRunEffectState.PREPARED,
        created_at=datetime(2026, 10, 7, tzinfo=UTC),
    )
    prepared_event = _stored_event(
        project_id=project_id,
        effect=effect,
        version=1,
        event_type=AgentRunEffectEventType.PREPARED,
        sequence=1,
    )
    snapshot = AgentRunEffectReplaySnapshot.from_state(
        replay_agent_run_effect([prepared_event]),
        project_seq=prepared_event.project_seq,
        created_at=prepared_event.recorded_at,
    )

    with pytest.raises(ReplaySnapshotIntegrityError, match="at or before its cursor"):
        replay_agent_run_effect_from_snapshot(snapshot, [prepared_event])
