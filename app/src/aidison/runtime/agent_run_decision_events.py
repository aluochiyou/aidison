"""Versioned human-review events for non-canonical AgentRun proposals."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from aidison.domain.events import (
    DomainEventMetadata,
    EventIntegrityError,
    EventSequenceGap,
    ReplaySnapshotIntegrityError,
    StoredDomainEvent,
    UnsupportedEventSchema,
    canonical_payload_hash,
    domain_event_fingerprint,
)
from aidison.domain.models import FrozenModel
from aidison.research.decision_contracts import AgentRunDecision, AgentRunDecisionStatus


class AgentRunDecisionEventType(StrEnum):
    PREPARED = "agent_run_decision.prepared"
    APPROVED = "agent_run_decision.approved"
    REJECTED = "agent_run_decision.rejected"


_AGENT_RUN_DECISION_EVENT_NAMESPACE = UUID("a4e902fd-cb1d-4ca2-bad5-f0c4fa7c97fc")


def agent_run_decision_event_id(aggregate_id: UUID, aggregate_version: int) -> UUID:
    return uuid5(_AGENT_RUN_DECISION_EVENT_NAMESPACE, f"{aggregate_id}:{aggregate_version}")


def agent_run_decision_event_metadata(
    *,
    decision: AgentRunDecision,
    aggregate_version: int,
    payload: dict[str, object],
    occurred_at: datetime,
) -> DomainEventMetadata:
    return DomainEventMetadata.for_payload(
        event_id=agent_run_decision_event_id(decision.id, aggregate_version),
        schema_version=1,
        aggregate_type="agent_run_decision",
        aggregate_id=decision.id,
        aggregate_version=aggregate_version,
        payload=payload,
        correlation_id=decision.agent_run_id,
        actor="application_service",
        source_component="agent_run_decision_store",
        artifact_refs=(decision.proposal_manifest_ref,),
        occurred_at=occurred_at,
    )


class AgentRunDecisionReplayState(FrozenModel):
    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    decision: dict[str, object]
    seen_event_ids: frozenset[UUID] = Field(default_factory=frozenset)
    event_fingerprints: dict[UUID, str] = Field(default_factory=dict)

    @property
    def agent_run_decision(self) -> AgentRunDecision:
        return AgentRunDecision.model_validate(self.decision)

    def normalized_payload(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "decision": self.decision,
            "seen_event_ids": sorted(str(event_id) for event_id in self.seen_event_ids),
            "event_fingerprints": {
                str(event_id): fingerprint
                for event_id, fingerprint in sorted(
                    self.event_fingerprints.items(), key=lambda item: str(item[0])
                )
            },
        }


_AGENT_RUN_DECISION_REPLAY_REDUCER_VERSION = "agent-run-decision-replay.v1"


class AgentRunDecisionReplaySnapshot(FrozenModel):
    """Immutable accelerator for one proposal review and its human verdict."""

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    project_seq: int = Field(ge=1)
    reducer_version: str = _AGENT_RUN_DECISION_REPLAY_REDUCER_VERSION
    state: dict[str, object]
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime

    @classmethod
    def from_state(
        cls,
        state: AgentRunDecisionReplayState,
        *,
        project_seq: int,
        created_at: datetime,
    ) -> AgentRunDecisionReplaySnapshot:
        payload = state.normalized_payload()
        return cls(
            project_id=state.project_id,
            aggregate_id=state.aggregate_id,
            aggregate_version=state.aggregate_version,
            project_seq=project_seq,
            state=payload,
            state_hash=canonical_payload_hash(payload),
            created_at=created_at,
        )

    @model_validator(mode="after")
    def validates_state_hash_and_identity(self) -> AgentRunDecisionReplaySnapshot:
        if self.reducer_version != _AGENT_RUN_DECISION_REPLAY_REDUCER_VERSION:
            raise ValueError("unsupported AgentRun decision replay reducer version")
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ValueError("AgentRun decision replay snapshot state hash mismatch")
        try:
            state = AgentRunDecisionReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ValueError("replay snapshot cannot restore AgentRun decision state") from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ValueError("AgentRun decision snapshot envelope differs from reducer state")
        return self

    def restore_state(self) -> AgentRunDecisionReplayState:
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ReplaySnapshotIntegrityError(
                "AgentRun decision replay snapshot state hash mismatch"
            )
        try:
            state = AgentRunDecisionReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ReplaySnapshotIntegrityError(
                "replay snapshot cannot restore AgentRun decision state"
            ) from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ReplaySnapshotIntegrityError(
                "AgentRun decision snapshot envelope differs from reducer state"
            )
        return state


def _event_decision(event: StoredDomainEvent) -> AgentRunDecision:
    raw_decision = event.payload.get("decision")
    if not isinstance(raw_decision, dict):
        raise EventIntegrityError("AgentRun decision event is missing the complete decision")
    decision = AgentRunDecision.model_validate(raw_decision)
    if decision.id != event.aggregate_id or decision.project_id != event.project_id:
        raise EventIntegrityError("AgentRun decision identity differs from event aggregate")
    return decision


def _same_authorization(left: AgentRunDecision, right: AgentRunDecision) -> bool:
    return (
        left.id == right.id
        and left.agent_run_id == right.agent_run_id
        and left.project_id == right.project_id
        and left.basis_hash == right.basis_hash
        and left.proposal_manifest_ref == right.proposal_manifest_ref
        and left.proposal_manifest_hash == right.proposal_manifest_hash
        and left.created_at == right.created_at
    )


def reduce_agent_run_decision_event(
    state: AgentRunDecisionReplayState | None,
    event: StoredDomainEvent,
) -> AgentRunDecisionReplayState:
    """Rebuild a prepared proposal review and its one human verdict."""

    if event.schema_version != 1:
        raise UnsupportedEventSchema(
            f"AgentRun decision schema {event.schema_version} is not supported"
        )
    if event.aggregate_type != "agent_run_decision":
        raise EventIntegrityError("event does not belong to an AgentRun decision aggregate")
    if canonical_payload_hash(event.payload) != event.payload_hash:
        raise EventIntegrityError("event payload hash mismatch")

    fingerprint = domain_event_fingerprint(event)
    if state is not None and event.event_id in state.seen_event_ids:
        if state.event_fingerprints.get(event.event_id) != fingerprint:
            raise EventIntegrityError("duplicate event ID contains different immutable content")
        return state
    if state is None:
        if event.aggregate_version != 1:
            raise EventSequenceGap(
                f"AgentRun decision stream must start at version 1, got {event.aggregate_version}"
            )
    else:
        if event.project_id != state.project_id or event.aggregate_id != state.aggregate_id:
            raise EventIntegrityError("AgentRun decision aggregate identity changed during replay")
        if event.aggregate_version != state.aggregate_version + 1:
            raise EventSequenceGap(
                f"expected AgentRun decision version {state.aggregate_version + 1}, "
                f"got {event.aggregate_version}"
            )

    try:
        event_type = AgentRunDecisionEventType(event.event_type)
    except ValueError as exc:
        raise EventIntegrityError("unsupported AgentRun decision lifecycle event") from exc
    decision = _event_decision(event)
    if state is None:
        if (
            event_type is not AgentRunDecisionEventType.PREPARED
            or decision.status is not AgentRunDecisionStatus.PENDING
        ):
            raise EventIntegrityError("AgentRun decision stream must start with pending prepared")
    else:
        previous = state.agent_run_decision
        if not _same_authorization(previous, decision):
            raise EventIntegrityError("AgentRun decision authorization changed during replay")
        expected_event = {
            AgentRunDecisionStatus.APPROVED: AgentRunDecisionEventType.APPROVED,
            AgentRunDecisionStatus.REJECTED: AgentRunDecisionEventType.REJECTED,
        }.get(decision.status)
        if previous.status is not AgentRunDecisionStatus.PENDING:
            raise EventIntegrityError("AgentRun decision is already resolved")
        if expected_event is not event_type or decision.answer != decision.status.value:
            raise EventIntegrityError("decision verdict does not match its event")

    return AgentRunDecisionReplayState(
        project_id=event.project_id,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        decision=decision.model_dump(mode="json"),
        seen_event_ids=(
            frozenset({event.event_id})
            if state is None
            else frozenset((*state.seen_event_ids, event.event_id))
        ),
        event_fingerprints=(
            {event.event_id: fingerprint}
            if state is None
            else {**state.event_fingerprints, event.event_id: fingerprint}
        ),
    )


def replay_agent_run_decision(
    events: list[StoredDomainEvent],
) -> AgentRunDecisionReplayState:
    state: AgentRunDecisionReplayState | None = None
    for event in events:
        state = reduce_agent_run_decision_event(state, event)
    if state is None:
        raise EventSequenceGap("AgentRun decision stream is empty")
    return state


def replay_agent_run_decision_from_snapshot(
    snapshot: AgentRunDecisionReplaySnapshot,
    tail_events: list[StoredDomainEvent],
) -> AgentRunDecisionReplayState:
    state = snapshot.restore_state()
    for event in tail_events:
        if (
            event.project_id != snapshot.project_id
            or event.aggregate_id != snapshot.aggregate_id
            or event.aggregate_type != "agent_run_decision"
        ):
            raise ReplaySnapshotIntegrityError(
                "AgentRun decision snapshot tail contains another aggregate"
            )
        if event.project_seq <= snapshot.project_seq:
            raise ReplaySnapshotIntegrityError(
                "AgentRun decision snapshot tail contains an event at or before its cursor"
            )
        if event.aggregate_version <= snapshot.aggregate_version:
            raise ReplaySnapshotIntegrityError(
                "AgentRun decision snapshot tail contains an event at or before its version"
            )
        state = reduce_agent_run_decision_event(state, event)
    return state


def agent_run_decision_replay_state_hash(state: AgentRunDecisionReplayState) -> str:
    return canonical_payload_hash(state.normalized_payload())


__all__ = [
    "AgentRunDecisionEventType",
    "AgentRunDecisionReplaySnapshot",
    "AgentRunDecisionReplayState",
    "agent_run_decision_event_id",
    "agent_run_decision_event_metadata",
    "agent_run_decision_replay_state_hash",
    "reduce_agent_run_decision_event",
    "replay_agent_run_decision",
    "replay_agent_run_decision_from_snapshot",
]
