"""Versioned ResultEnvelope and admission events with pure replay."""

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
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ResultEnvelope,
    validate_result_admission,
)


class AgentResultEventType(StrEnum):
    RECORDED = "agent_result.recorded"
    ACCEPTED = "agent_result.accepted"
    QUARANTINED = "agent_result.quarantined"
    REJECTED = "agent_result.rejected"


_AGENT_RESULT_EVENT_NAMESPACE = UUID("d8452c0b-1a3a-4ce0-a64f-b8dca3bfa004")


def agent_result_event_id(aggregate_id: UUID, aggregate_version: int) -> UUID:
    """Derive a stable event ID from immutable ResultEnvelope identity."""

    return uuid5(_AGENT_RESULT_EVENT_NAMESPACE, f"{aggregate_id}:{aggregate_version}")


def agent_result_event_metadata(
    *,
    result: ResultEnvelope,
    aggregate_version: int,
    payload: dict[str, object],
    occurred_at: datetime,
    artifact_refs: tuple[str, ...] = (),
) -> DomainEventMetadata:
    return DomainEventMetadata.for_payload(
        event_id=agent_result_event_id(result.id, aggregate_version),
        schema_version=1,
        aggregate_type="agent_run_result",
        aggregate_id=result.id,
        aggregate_version=aggregate_version,
        payload=payload,
        correlation_id=result.run_id,
        actor="application_service",
        source_component="agent_result_store",
        artifact_refs=artifact_refs,
        occurred_at=occurred_at,
    )


class AgentResultReplayState(FrozenModel):
    """One immutable producer result plus its optional Control verdict."""

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    result: dict[str, object]
    admission: dict[str, object] | None = None
    seen_event_ids: frozenset[UUID] = Field(default_factory=frozenset)
    event_fingerprints: dict[UUID, str] = Field(default_factory=dict)

    @property
    def result_envelope(self) -> ResultEnvelope:
        return ResultEnvelope.model_validate(self.result)

    @property
    def admission_record(self) -> AdmissionRecord | None:
        return AdmissionRecord.model_validate(self.admission) if self.admission else None

    def normalized_payload(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "result": self.result,
            "admission": self.admission,
            "seen_event_ids": sorted(str(event_id) for event_id in self.seen_event_ids),
            "event_fingerprints": {
                str(event_id): fingerprint
                for event_id, fingerprint in sorted(
                    self.event_fingerprints.items(), key=lambda item: str(item[0])
                )
            },
        }


_AGENT_RESULT_REPLAY_REDUCER_VERSION = "agent-run-result-replay.v1"


class AgentResultReplaySnapshot(FrozenModel):
    """Immutable accelerator for one result envelope and its admission verdict."""

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    project_seq: int = Field(ge=1)
    reducer_version: str = _AGENT_RESULT_REPLAY_REDUCER_VERSION
    state: dict[str, object]
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime

    @classmethod
    def from_state(
        cls,
        state: AgentResultReplayState,
        *,
        project_seq: int,
        created_at: datetime,
    ) -> AgentResultReplaySnapshot:
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
    def validates_state_hash_and_identity(self) -> AgentResultReplaySnapshot:
        if self.reducer_version != _AGENT_RESULT_REPLAY_REDUCER_VERSION:
            raise ValueError("unsupported AgentRun result replay reducer version")
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ValueError("AgentRun result replay snapshot state hash mismatch")
        try:
            state = AgentResultReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ValueError("replay snapshot cannot restore AgentRun result state") from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ValueError("AgentRun result snapshot envelope differs from reducer state")
        return self

    def restore_state(self) -> AgentResultReplayState:
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ReplaySnapshotIntegrityError(
                "AgentRun result replay snapshot state hash mismatch"
            )
        try:
            state = AgentResultReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ReplaySnapshotIntegrityError(
                "replay snapshot cannot restore AgentRun result state"
            ) from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ReplaySnapshotIntegrityError(
                "AgentRun result snapshot envelope differs from reducer state"
            )
        return state


def _admission_event_type(disposition: AdmissionDisposition) -> AgentResultEventType:
    return {
        AdmissionDisposition.ACCEPTED: AgentResultEventType.ACCEPTED,
        AdmissionDisposition.QUARANTINED: AgentResultEventType.QUARANTINED,
        AdmissionDisposition.REJECTED: AgentResultEventType.REJECTED,
    }[disposition]


def _event_result(event: StoredDomainEvent) -> ResultEnvelope:
    raw_result = event.payload.get("result")
    if not isinstance(raw_result, dict):
        raise EventIntegrityError("AgentRun result event is missing the complete result")
    result = ResultEnvelope.model_validate(raw_result)
    if result.id != event.aggregate_id:
        raise EventIntegrityError("result identity differs from event aggregate")
    return result


def reduce_agent_result_event(
    state: AgentResultReplayState | None,
    event: StoredDomainEvent,
) -> AgentResultReplayState:
    """Apply a result lifecycle event without reading PostgreSQL or artifacts."""

    if event.schema_version != 1:
        raise UnsupportedEventSchema(
            f"AgentRun result schema {event.schema_version} is not supported"
        )
    if event.aggregate_type != "agent_run_result":
        raise EventIntegrityError("event does not belong to an AgentRun result aggregate")
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
                f"AgentRun result stream must start at version 1, got {event.aggregate_version}"
            )
    else:
        if event.project_id != state.project_id or event.aggregate_id != state.aggregate_id:
            raise EventIntegrityError("AgentRun result aggregate identity changed during replay")
        if event.aggregate_version != state.aggregate_version + 1:
            raise EventSequenceGap(
                f"expected AgentRun result version {state.aggregate_version + 1}, "
                f"got {event.aggregate_version}"
            )

    try:
        event_type = AgentResultEventType(event.event_type)
    except ValueError as exc:
        raise EventIntegrityError("unsupported AgentRun result lifecycle event") from exc
    result = _event_result(event)
    if state is None:
        if event_type is not AgentResultEventType.RECORDED:
            raise EventIntegrityError("AgentRun result stream must start with recorded")
        return AgentResultReplayState(
            project_id=event.project_id,
            aggregate_id=event.aggregate_id,
            aggregate_version=event.aggregate_version,
            result=result.model_dump(mode="json"),
            seen_event_ids=frozenset({event.event_id}),
            event_fingerprints={event.event_id: fingerprint},
        )

    if event_type is AgentResultEventType.RECORDED:
        raise EventIntegrityError("AgentRun result cannot be recorded twice")
    if state.admission is not None:
        raise EventIntegrityError("AgentRun result already has an admission verdict")
    if result != state.result_envelope:
        raise EventIntegrityError("admission event changed immutable producer result")
    raw_admission = event.payload.get("admission")
    if not isinstance(raw_admission, dict):
        raise EventIntegrityError("admission event is missing the complete admission")
    admission = AdmissionRecord.model_validate(raw_admission)
    if event_type is not _admission_event_type(admission.disposition):
        raise EventIntegrityError("admission event type differs from its disposition")
    try:
        validate_result_admission(result=result, admission=admission)
    except ValueError as exc:
        raise EventIntegrityError(str(exc)) from exc
    return AgentResultReplayState(
        project_id=event.project_id,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        result=result.model_dump(mode="json"),
        admission=admission.model_dump(mode="json"),
        seen_event_ids=frozenset((*state.seen_event_ids, event.event_id)),
        event_fingerprints={**state.event_fingerprints, event.event_id: fingerprint},
    )


def replay_agent_result(events: list[StoredDomainEvent]) -> AgentResultReplayState:
    """Rebuild one ResultEnvelope and its sole durable admission verdict."""

    state: AgentResultReplayState | None = None
    for event in events:
        state = reduce_agent_result_event(state, event)
    if state is None:
        raise EventSequenceGap("AgentRun result stream is empty")
    return state


def replay_agent_result_from_snapshot(
    snapshot: AgentResultReplaySnapshot,
    tail_events: list[StoredDomainEvent],
) -> AgentResultReplayState:
    state = snapshot.restore_state()
    for event in tail_events:
        if (
            event.project_id != snapshot.project_id
            or event.aggregate_id != snapshot.aggregate_id
            or event.aggregate_type != "agent_run_result"
        ):
            raise ReplaySnapshotIntegrityError(
                "AgentRun result snapshot tail contains another aggregate"
            )
        if event.project_seq <= snapshot.project_seq:
            raise ReplaySnapshotIntegrityError(
                "AgentRun result snapshot tail contains an event at or before its cursor"
            )
        if event.aggregate_version <= snapshot.aggregate_version:
            raise ReplaySnapshotIntegrityError(
                "AgentRun result snapshot tail contains an event at or before its version"
            )
        state = reduce_agent_result_event(state, event)
    return state


def agent_result_replay_state_hash(state: AgentResultReplayState) -> str:
    return canonical_payload_hash(state.normalized_payload())


__all__ = [
    "AgentResultEventType",
    "AgentResultReplaySnapshot",
    "AgentResultReplayState",
    "agent_result_event_id",
    "agent_result_event_metadata",
    "agent_result_replay_state_hash",
    "reduce_agent_result_event",
    "replay_agent_result",
    "replay_agent_result_from_snapshot",
]
