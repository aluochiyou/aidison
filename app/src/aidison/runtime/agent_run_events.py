"""Versioned AgentRun lifecycle events and their pure replay reducer."""

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
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus


class AgentRunEventType(StrEnum):
    RESEARCH_QUEUED = "agent_run.queued"
    SOLUTION_QUEUED = "solution_run.queued"
    IMPACT_QUEUED = "impact_run.queued"
    RUNNING = "agent_run.running"
    CHECKPOINT_ADMITTED = "agent_run.checkpoint_admitted"
    WAITING = "agent_run.waiting"
    REQUEUED = "agent_run.requeued"
    CANCELLATION_REQUESTED = "agent_run.cancellation_requested"
    SUCCEEDED = "agent_run.succeeded"
    FAILED = "agent_run.failed"
    CANCELLED = "agent_run.cancelled"


_AGENT_RUN_EVENT_NAMESPACE = UUID("baab72d2-c876-4e74-9cf4-6fd5f00f72a2")


def agent_run_event_id(aggregate_id: UUID, aggregate_version: int) -> UUID:
    """Derive one stable lifecycle event identity for idempotent command retry."""
    return uuid5(_AGENT_RUN_EVENT_NAMESPACE, f"{aggregate_id}:{aggregate_version}")


def queued_agent_run_metadata(
    *,
    run: AgentRun,
    payload: dict[str, object],
    artifact_refs: tuple[str, ...] = (),
) -> DomainEventMetadata:
    """Build the schema-1 envelope for a newly authorized AgentRun."""
    return agent_run_lifecycle_metadata(
        run=run,
        aggregate_version=1,
        payload=payload,
        artifact_refs=artifact_refs,
    )


def agent_run_lifecycle_metadata(
    *,
    run: AgentRun,
    aggregate_version: int,
    payload: dict[str, object],
    artifact_refs: tuple[str, ...] = (),
) -> DomainEventMetadata:
    """Build one immutable envelope for a durable AgentRun transition."""
    return DomainEventMetadata.for_payload(
        event_id=agent_run_event_id(run.id, aggregate_version),
        schema_version=1,
        aggregate_type="agent_run",
        aggregate_id=run.id,
        aggregate_version=aggregate_version,
        payload=payload,
        correlation_id=run.id,
        actor="application_service",
        source_component="agent_run_application",
        artifact_refs=artifact_refs,
        occurred_at=run.updated_at,
    )


class AgentRunReplayState(FrozenModel):
    """Normalized AgentRun state rebuilt from its lifecycle stream."""

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    run: dict[str, object]
    seen_event_ids: frozenset[UUID] = Field(default_factory=frozenset)
    event_fingerprints: dict[UUID, str] = Field(default_factory=dict)

    @property
    def agent_run(self) -> AgentRun:
        return AgentRun.model_validate(self.run)

    def normalized_payload(self) -> dict[str, object]:
        """Return deterministic JSON-safe state for an immutable replay snapshot."""
        return {
            "project_id": str(self.project_id),
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "run": self.run,
            "seen_event_ids": sorted(str(event_id) for event_id in self.seen_event_ids),
            "event_fingerprints": {
                str(event_id): fingerprint
                for event_id, fingerprint in sorted(
                    self.event_fingerprints.items(), key=lambda item: str(item[0])
                )
            },
        }


_AGENT_RUN_REPLAY_REDUCER_VERSION = "agent-run-replay.v1"


class AgentRunReplaySnapshot(FrozenModel):
    """Validated replay accelerator for one AgentRun lifecycle stream.

    It captures only the reducer's PostgreSQL control-plane state. LangGraph
    GraphState remains exclusively in the checkpoint store and is never copied
    into this snapshot.
    """

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    project_seq: int = Field(ge=1)
    reducer_version: str = _AGENT_RUN_REPLAY_REDUCER_VERSION
    state: dict[str, object]
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime

    @classmethod
    def from_state(
        cls,
        state: AgentRunReplayState,
        *,
        project_seq: int,
        created_at: datetime,
    ) -> AgentRunReplaySnapshot:
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
    def validates_state_hash_and_identity(self) -> AgentRunReplaySnapshot:
        if self.reducer_version != _AGENT_RUN_REPLAY_REDUCER_VERSION:
            raise ValueError("unsupported AgentRun replay reducer version")
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ValueError("AgentRun replay snapshot state hash mismatch")
        try:
            state = AgentRunReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ValueError("replay snapshot cannot restore AgentRun state") from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ValueError("AgentRun replay snapshot envelope differs from reducer state")
        return self

    def restore_state(self) -> AgentRunReplayState:
        """Restore state only after repeating persisted snapshot integrity checks."""
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ReplaySnapshotIntegrityError("AgentRun replay snapshot state hash mismatch")
        try:
            state = AgentRunReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ReplaySnapshotIntegrityError(
                "replay snapshot cannot restore AgentRun state"
            ) from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ReplaySnapshotIntegrityError(
                "AgentRun replay snapshot envelope differs from reducer state"
            )
        return state


def _expected_queued_event(kind: AgentRunKind) -> AgentRunEventType:
    if kind is AgentRunKind.SOLUTION:
        return AgentRunEventType.SOLUTION_QUEUED
    if kind is AgentRunKind.IMPACT:
        return AgentRunEventType.IMPACT_QUEUED
    return AgentRunEventType.RESEARCH_QUEUED


def _same_agent_run_identity(left: AgentRun, right: AgentRun) -> bool:
    """Compare immutable run authorization fields, excluding lifecycle state."""
    return (
        left.id == right.id
        and left.project_id == right.project_id
        and left.kind == right.kind
        and left.idempotency_key == right.idempotency_key
        and left.basis_hash == right.basis_hash
        and left.basis_project_revision == right.basis_project_revision
        and left.runtime_binding == right.runtime_binding
        and left.thread_id == right.thread_id
        and left.run_contract_ref == right.run_contract_ref
        and left.coverage_contract_ref == right.coverage_contract_ref
        and left.created_at == right.created_at
    )


def _validate_transition(
    *,
    previous: AgentRun,
    current: AgentRun,
    event_type: AgentRunEventType,
    event_project_seq: int,
) -> None:
    if not _same_agent_run_identity(previous, current):
        raise EventIntegrityError("AgentRun immutable authorization fields changed")
    if event_type is AgentRunEventType.RUNNING:
        if previous.status not in {AgentRunStatus.QUEUED, AgentRunStatus.RUNNING}:
            raise EventIntegrityError("only queued or expired running AgentRun can be claimed")
        if current.status is not AgentRunStatus.RUNNING:
            raise EventIntegrityError("running event must contain a running AgentRun")
        if current.current_generation != previous.current_generation + 1:
            raise EventIntegrityError("running event must advance the claim generation")
        if current.cancel_requested:
            raise EventIntegrityError("claimed AgentRun cannot already be cancellation-requested")
        return
    if event_type is AgentRunEventType.CHECKPOINT_ADMITTED:
        if (
            previous.status is not AgentRunStatus.RUNNING
            or current.status is not AgentRunStatus.RUNNING
            or current.current_generation != previous.current_generation
            or current.admitted_checkpoint is None
        ):
            raise EventIntegrityError("checkpoint event must preserve an active AgentRun claim")
        if (
            current.admitted_checkpoint.event_cursor != 0
            and current.admitted_checkpoint.event_cursor != event_project_seq
        ):
            raise EventIntegrityError(
                "checkpoint replay cursor must equal its lifecycle event cursor"
            )
        return
    if event_type is AgentRunEventType.WAITING:
        if (
            previous.status is not AgentRunStatus.RUNNING
            or current.status is not AgentRunStatus.WAITING
        ):
            raise EventIntegrityError("waiting event must move a running AgentRun to waiting")
        return
    if event_type is AgentRunEventType.REQUEUED:
        if (
            previous.status is not AgentRunStatus.WAITING
            or current.status is not AgentRunStatus.QUEUED
        ):
            raise EventIntegrityError("requeued event must move a waiting AgentRun to queued")
        if current.admitted_checkpoint is not None:
            raise EventIntegrityError("checkpointed AgentRun cannot use pause requeue")
        return
    if event_type is AgentRunEventType.CANCELLATION_REQUESTED:
        if (
            previous.status is not AgentRunStatus.RUNNING
            or current.status is not AgentRunStatus.RUNNING
        ):
            raise EventIntegrityError("cancellation request must retain a running AgentRun")
        if (
            current.current_generation != previous.current_generation
            or not current.cancel_requested
        ):
            raise EventIntegrityError("cancellation request has an invalid AgentRun state")
        return

    expected_status = {
        AgentRunEventType.SUCCEEDED: AgentRunStatus.SUCCEEDED,
        AgentRunEventType.FAILED: AgentRunStatus.FAILED,
        AgentRunEventType.CANCELLED: AgentRunStatus.CANCELLED,
    }.get(event_type)
    if expected_status is None:
        raise EventIntegrityError("unsupported AgentRun lifecycle event")
    if previous.status not in {
        AgentRunStatus.QUEUED,
        AgentRunStatus.RUNNING,
        AgentRunStatus.WAITING,
    }:
        raise EventIntegrityError("terminal event follows an already terminal AgentRun")
    if current.status is not expected_status or current.completed_at is None:
        raise EventIntegrityError("terminal AgentRun event contains an invalid final state")


def reduce_agent_run_event(
    state: AgentRunReplayState | None,
    event: StoredDomainEvent,
) -> AgentRunReplayState:
    """Apply one AgentRun event without consulting PostgreSQL or LangGraph."""
    if event.schema_version != 1:
        raise UnsupportedEventSchema(f"AgentRun schema {event.schema_version} is not supported")
    if event.aggregate_type != "agent_run":
        raise EventIntegrityError("event does not belong to an AgentRun aggregate")
    if canonical_payload_hash(event.payload) != event.payload_hash:
        raise EventIntegrityError("event payload hash mismatch")

    fingerprint = domain_event_fingerprint(event)
    if state is not None and event.event_id in state.seen_event_ids:
        if state.event_fingerprints.get(event.event_id) != fingerprint:
            raise EventIntegrityError("duplicate event ID contains different immutable content")
        return state
    if state is not None:
        if event.project_id != state.project_id or event.aggregate_id != state.aggregate_id:
            raise EventIntegrityError("AgentRun aggregate identity changed during replay")
        if event.aggregate_version != state.aggregate_version + 1:
            raise EventSequenceGap(
                f"expected AgentRun version {state.aggregate_version + 1}, "
                f"got {event.aggregate_version}"
            )
    elif event.aggregate_version != 1:
        raise EventSequenceGap(
            f"AgentRun stream must start at version 1, got {event.aggregate_version}"
        )

    raw_run = event.payload.get("run")
    if not isinstance(raw_run, dict):
        raise EventIntegrityError("queued AgentRun event is missing the complete run")
    run = AgentRun.model_validate(raw_run)
    if run.id != event.aggregate_id or run.project_id != event.project_id:
        raise EventIntegrityError("queued AgentRun identity differs from event identity")
    try:
        typed_event = AgentRunEventType(event.event_type)
    except ValueError as exc:
        raise EventIntegrityError("unsupported AgentRun lifecycle event") from exc
    if state is None:
        if run.status is not AgentRunStatus.QUEUED or run.current_generation != 0:
            raise EventIntegrityError("queued AgentRun event contains an already-started run")
        expected_event = _expected_queued_event(run.kind)
        if typed_event is not expected_event:
            raise EventIntegrityError("queued event type does not match AgentRun kind")
    else:
        _validate_transition(
            previous=state.agent_run,
            current=run,
            event_type=typed_event,
            event_project_seq=event.project_seq,
        )

    return AgentRunReplayState(
        project_id=event.project_id,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        run=run.model_dump(mode="json"),
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


def replay_agent_run(events: list[StoredDomainEvent]) -> AgentRunReplayState:
    """Rebuild the supported prefix of one AgentRun lifecycle stream."""
    state: AgentRunReplayState | None = None
    for event in events:
        state = reduce_agent_run_event(state, event)
    if state is None:
        raise EventSequenceGap("AgentRun stream is empty")
    return state


def replay_agent_run_from_snapshot(
    snapshot: AgentRunReplaySnapshot,
    tail_events: list[StoredDomainEvent],
) -> AgentRunReplayState:
    """Apply only verified AgentRun lifecycle events after a saved snapshot."""
    state = snapshot.restore_state()
    for event in tail_events:
        if (
            event.project_id != snapshot.project_id
            or event.aggregate_id != snapshot.aggregate_id
            or event.aggregate_type != "agent_run"
        ):
            raise ReplaySnapshotIntegrityError("AgentRun snapshot tail contains another aggregate")
        if event.project_seq <= snapshot.project_seq:
            raise ReplaySnapshotIntegrityError(
                "AgentRun snapshot tail contains an event at or before its cursor"
            )
        if event.aggregate_version <= snapshot.aggregate_version:
            raise ReplaySnapshotIntegrityError(
                "AgentRun snapshot tail contains an event at or before its version"
            )
        state = reduce_agent_run_event(state, event)
    return state


def agent_run_replay_state_hash(state: AgentRunReplayState) -> str:
    """Hash the normalized state used by full and snapshot-tail AgentRun replay."""
    return canonical_payload_hash(state.normalized_payload())


__all__ = [
    "AgentRunEventType",
    "AgentRunReplaySnapshot",
    "AgentRunReplayState",
    "agent_run_event_id",
    "agent_run_lifecycle_metadata",
    "agent_run_replay_state_hash",
    "queued_agent_run_metadata",
    "reduce_agent_run_event",
    "replay_agent_run",
    "replay_agent_run_from_snapshot",
]
