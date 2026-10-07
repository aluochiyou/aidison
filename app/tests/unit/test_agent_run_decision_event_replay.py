from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.domain.events import (
    EventIntegrityError,
    ReplaySnapshotIntegrityError,
    StoredDomainEvent,
)
from aidison.research.decision_contracts import AgentRunDecision, AgentRunDecisionStatus
from aidison.runtime.agent_run_decision_events import (
    AgentRunDecisionEventType,
    AgentRunDecisionReplaySnapshot,
    agent_run_decision_event_metadata,
    agent_run_decision_replay_state_hash,
    replay_agent_run_decision,
    replay_agent_run_decision_from_snapshot,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _stored_event(
    *,
    decision: AgentRunDecision,
    version: int,
    event_type: AgentRunDecisionEventType,
    sequence: int,
) -> StoredDomainEvent:
    payload: dict[str, object] = {"decision": decision.model_dump(mode="json")}
    occurred_at = decision.resolved_at or decision.created_at
    metadata = agent_run_decision_event_metadata(
        decision=decision,
        aggregate_version=version,
        payload=payload,
        occurred_at=occurred_at,
    )
    return StoredDomainEvent(
        **metadata.model_dump(),
        project_id=decision.project_id,
        project_seq=sequence,
        event_type=event_type.value,
        payload=payload,
        recorded_at=datetime(2026, 10, 7, tzinfo=UTC),
    )


def test_decision_replay_preserves_proposal_binding_and_one_human_verdict() -> None:
    decision = AgentRunDecision(
        project_id=uuid4(),
        agent_run_id=uuid4(),
        basis_hash=_hash("basis"),
        proposal_manifest_ref="artifact+sha256://proposal/manifest.json",
        proposal_manifest_hash=_hash("proposal"),
    )
    approved = decision.model_copy(
        update={
            "status": AgentRunDecisionStatus.APPROVED,
            "answer": AgentRunDecisionStatus.APPROVED.value,
            "resolved_at": datetime(2026, 10, 7, tzinfo=UTC),
        }
    )

    state = replay_agent_run_decision(
        [
            _stored_event(
                decision=decision,
                version=1,
                event_type=AgentRunDecisionEventType.PREPARED,
                sequence=1,
            ),
            _stored_event(
                decision=approved,
                version=2,
                event_type=AgentRunDecisionEventType.APPROVED,
                sequence=2,
            ),
        ]
    )

    assert state.agent_run_decision == approved


def test_decision_snapshot_tail_replay_preserves_human_verdict() -> None:
    decision = AgentRunDecision(
        project_id=uuid4(),
        agent_run_id=uuid4(),
        basis_hash=_hash("basis"),
        proposal_manifest_ref="artifact+sha256://proposal/manifest.json",
        proposal_manifest_hash=_hash("proposal"),
    )
    approved = decision.model_copy(
        update={
            "status": AgentRunDecisionStatus.APPROVED,
            "answer": AgentRunDecisionStatus.APPROVED.value,
            "resolved_at": datetime(2026, 10, 7, tzinfo=UTC),
        }
    )
    prepared_event = _stored_event(
        decision=decision,
        version=1,
        event_type=AgentRunDecisionEventType.PREPARED,
        sequence=1,
    )
    approved_event = _stored_event(
        decision=approved,
        version=2,
        event_type=AgentRunDecisionEventType.APPROVED,
        sequence=2,
    )
    snapshot = AgentRunDecisionReplaySnapshot.from_state(
        replay_agent_run_decision([prepared_event]),
        project_seq=prepared_event.project_seq,
        created_at=prepared_event.recorded_at,
    )

    full = replay_agent_run_decision([prepared_event, approved_event])
    accelerated = replay_agent_run_decision_from_snapshot(snapshot, [approved_event])

    assert accelerated == full
    assert accelerated.agent_run_decision == approved
    assert agent_run_decision_replay_state_hash(accelerated) == (
        agent_run_decision_replay_state_hash(full)
    )


def test_decision_snapshot_tail_replay_rejects_an_event_at_or_before_its_cursor() -> None:
    decision = AgentRunDecision(
        project_id=uuid4(),
        agent_run_id=uuid4(),
        basis_hash=_hash("basis"),
        proposal_manifest_ref="artifact+sha256://proposal/manifest.json",
        proposal_manifest_hash=_hash("proposal"),
    )
    prepared_event = _stored_event(
        decision=decision,
        version=1,
        event_type=AgentRunDecisionEventType.PREPARED,
        sequence=1,
    )
    snapshot = AgentRunDecisionReplaySnapshot.from_state(
        replay_agent_run_decision([prepared_event]),
        project_seq=prepared_event.project_seq,
        created_at=prepared_event.recorded_at,
    )

    with pytest.raises(ReplaySnapshotIntegrityError, match="at or before its cursor"):
        replay_agent_run_decision_from_snapshot(snapshot, [prepared_event])


def test_decision_replay_rejects_a_verdict_with_the_wrong_event_type() -> None:
    decision = AgentRunDecision(
        project_id=uuid4(),
        agent_run_id=uuid4(),
        basis_hash=_hash("basis"),
        proposal_manifest_ref="artifact+sha256://proposal/manifest.json",
        proposal_manifest_hash=_hash("proposal"),
    )
    rejected = decision.model_copy(
        update={
            "status": AgentRunDecisionStatus.REJECTED,
            "answer": AgentRunDecisionStatus.REJECTED.value,
            "resolved_at": datetime(2026, 10, 7, tzinfo=UTC),
        }
    )

    with pytest.raises(EventIntegrityError, match="does not match"):
        replay_agent_run_decision(
            [
                _stored_event(
                    decision=decision,
                    version=1,
                    event_type=AgentRunDecisionEventType.PREPARED,
                    sequence=1,
                ),
                _stored_event(
                    decision=rejected,
                    version=2,
                    event_type=AgentRunDecisionEventType.APPROVED,
                    sequence=2,
                ),
            ]
        )
