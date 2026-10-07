"""Versioned domain-event contracts and pure replay reducers.

The relational domain model remains authoritative while event-sourced
projections are introduced one aggregate at a time.  Nothing in this module
performs I/O; the same input stream must always produce the same state or the
same integrity failure.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from pydantic import Field, model_validator

from aidison.domain.models import ExecutionPlanProposal, ExecutionPlanStatus, FrozenModel


class EventReplayError(RuntimeError):
    """Base class for deterministic replay failures."""


class EventSequenceGap(EventReplayError):
    """An aggregate stream skipped or reordered a required version."""


class UnsupportedEventSchema(EventReplayError):
    """The reducer has no deterministic upcast path for this schema."""


class EventIntegrityError(EventReplayError):
    """Stored event identity or content is inconsistent."""


class ReplaySnapshotIntegrityError(EventReplayError):
    """A replay acceleration record cannot safely restore reducer state."""


class ExecutionPlanEventType(StrEnum):
    PROPOSED = "execution_plan.proposed"
    RESOLVED = "execution_plan.resolved"


_EXECUTION_PLAN_EVENT_NAMESPACE = UUID("6a4e4ed4-5554-4cbd-8a80-bbdd17c27c74")


def execution_plan_event_id(aggregate_id: UUID, aggregate_version: int) -> UUID:
    """Derive the stable lifecycle event ID used by retries and causation links."""
    return uuid5(_EXECUTION_PLAN_EVENT_NAMESPACE, f"{aggregate_id}:{aggregate_version}")


def canonical_payload_hash(payload: dict[str, object]) -> str:
    """Hash one JSON payload with stable key and separator rules."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class DomainEventMetadata(FrozenModel):
    """Identity and integrity fields stored beside one event payload."""

    event_id: UUID = Field(default_factory=uuid4)
    schema_version: int = Field(ge=1)
    aggregate_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    correlation_id: UUID
    causation_id: UUID | None = None
    actor: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    source_component: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @classmethod
    def for_payload(
        cls,
        *,
        schema_version: int,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int,
        payload: dict[str, object],
        correlation_id: UUID,
        actor: str,
        source_component: str,
        event_id: UUID | None = None,
        causation_id: UUID | None = None,
        artifact_refs: tuple[str, ...] = (),
        occurred_at: datetime | None = None,
    ) -> DomainEventMetadata:
        return cls(
            event_id=event_id or uuid4(),
            schema_version=schema_version,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            occurred_at=occurred_at or datetime.now(UTC),
            payload_hash=canonical_payload_hash(payload),
            correlation_id=correlation_id,
            causation_id=causation_id,
            actor=actor,
            source_component=source_component,
            artifact_refs=artifact_refs,
        )


class StoredDomainEvent(DomainEventMetadata):
    """A versioned event read from the ordered PostgreSQL project stream."""

    project_id: UUID
    project_seq: int = Field(ge=1)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,199}$")
    payload: dict[str, object]
    recorded_at: datetime

    @model_validator(mode="after")
    def aggregate_matches_supported_event(self) -> StoredDomainEvent:
        if self.aggregate_type == "execution_plan" and not self.event_type.startswith(
            "execution_plan."
        ):
            raise ValueError("execution-plan aggregate contains another event family")
        return self


EventUpcaster = Callable[[dict[str, object]], dict[str, object]]


class EventUpcasterRegistry:
    """Apply explicit, adjacent schema upgrades without mutating stored events.

    Upcasters only transform the payload. Event identity, aggregate ordering,
    causation and storage timestamps remain immutable. A missing intermediate
    step fails closed instead of guessing how to interpret historical data.
    """

    def __init__(self) -> None:
        self._steps: dict[tuple[str, int], EventUpcaster] = {}

    def register(
        self,
        *,
        event_type: str,
        from_version: int,
        upcaster: EventUpcaster,
    ) -> None:
        if from_version < 1:
            raise ValueError("upcaster source version must be positive")
        key = (event_type, from_version)
        if key in self._steps:
            raise ValueError(
                f"upcaster already registered for {event_type} schema {from_version}"
            )
        self._steps[key] = upcaster

    def upcast(
        self,
        event: StoredDomainEvent,
        *,
        target_version: int,
    ) -> StoredDomainEvent:
        if target_version < event.schema_version:
            raise UnsupportedEventSchema(
                f"cannot downcast {event.event_type} schema "
                f"{event.schema_version} to {target_version}"
            )
        if canonical_payload_hash(event.payload) != event.payload_hash:
            raise EventIntegrityError("event payload hash mismatch before upcast")
        if target_version == event.schema_version:
            return event

        version = event.schema_version
        payload = deepcopy(event.payload)
        while version < target_version:
            upcaster = self._steps.get((event.event_type, version))
            if upcaster is None:
                raise UnsupportedEventSchema(
                    f"no upcaster for {event.event_type} schema {version}"
                )
            upgraded = upcaster(deepcopy(payload))
            if not isinstance(upgraded, dict):
                raise EventIntegrityError("event upcaster must return an object payload")
            payload = deepcopy(upgraded)
            version += 1

        return event.model_copy(
            update={
                "schema_version": target_version,
                "payload": payload,
                "payload_hash": canonical_payload_hash(payload),
            }
        )


class ExecutionPlanReplayState(FrozenModel):
    """Normalized relation-shaped state rebuilt from plan lifecycle events."""

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    plan: dict[str, object]
    seen_event_ids: frozenset[UUID] = Field(default_factory=frozenset)
    event_ids_by_version: dict[int, UUID] = Field(default_factory=dict)
    event_fingerprints: dict[UUID, str] = Field(default_factory=dict)

    @property
    def proposal(self) -> ExecutionPlanProposal:
        return ExecutionPlanProposal.model_validate(self.plan)

    def normalized_payload(self) -> dict[str, object]:
        """Return a deterministic, JSON-safe representation for a replay snapshot.

        ``frozenset`` iteration is intentionally unordered, so directly hashing
        ``model_dump`` would make an otherwise identical checkpoint appear to
        change between processes.  Snapshot payloads therefore sort every
        set-like identity collection before hashing or persistence.
        """
        return {
            "project_id": str(self.project_id),
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "plan": self.plan,
            "seen_event_ids": sorted(str(event_id) for event_id in self.seen_event_ids),
            "event_ids_by_version": {
                str(version): str(event_id)
                for version, event_id in sorted(self.event_ids_by_version.items())
            },
            "event_fingerprints": {
                str(event_id): fingerprint
                for event_id, fingerprint in sorted(
                    self.event_fingerprints.items(), key=lambda item: str(item[0])
                )
            },
        }


_EXECUTION_PLAN_REPLAY_REDUCER_VERSION = "execution-plan-replay.v1"


class ExecutionPlanReplaySnapshot(FrozenModel):
    """Validated accelerator for one execution-plan event stream.

    It is deliberately not a second source of truth: callers must compare
    replayed state with the canonical relational projection before creating it,
    and may always discard and rebuild it from domain events.
    """

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    project_seq: int = Field(ge=1)
    reducer_version: str = _EXECUTION_PLAN_REPLAY_REDUCER_VERSION
    state: dict[str, object]
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_state(
        cls,
        state: ExecutionPlanReplayState,
        *,
        project_seq: int,
    ) -> ExecutionPlanReplaySnapshot:
        payload = state.normalized_payload()
        return cls(
            project_id=state.project_id,
            aggregate_id=state.aggregate_id,
            aggregate_version=state.aggregate_version,
            project_seq=project_seq,
            state=payload,
            state_hash=canonical_payload_hash(payload),
        )

    @model_validator(mode="after")
    def validates_state_hash_and_identity(self) -> ExecutionPlanReplaySnapshot:
        if self.reducer_version != _EXECUTION_PLAN_REPLAY_REDUCER_VERSION:
            raise ValueError("unsupported execution-plan replay reducer version")
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ValueError("replay snapshot state hash mismatch")
        try:
            state = ExecutionPlanReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ValueError("replay snapshot state cannot restore execution-plan state") from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ValueError("replay snapshot envelope differs from reducer state")
        return self

    def restore_state(self) -> ExecutionPlanReplayState:
        """Restore reducer state only after rechecking persisted integrity."""
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ReplaySnapshotIntegrityError("replay snapshot state hash mismatch")
        try:
            state = ExecutionPlanReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ReplaySnapshotIntegrityError(
                "replay snapshot state cannot restore execution-plan state"
            ) from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ReplaySnapshotIntegrityError(
                "replay snapshot envelope differs from reducer state"
            )
        return state


def _validate_event_integrity(event: StoredDomainEvent) -> None:
    if event.schema_version != 1:
        raise UnsupportedEventSchema(
            f"execution-plan schema {event.schema_version} is not supported"
        )
    if event.aggregate_type != "execution_plan":
        raise EventIntegrityError("event does not belong to an execution-plan aggregate")
    if canonical_payload_hash(event.payload) != event.payload_hash:
        raise EventIntegrityError("event payload hash mismatch")


def domain_event_fingerprint(event: StoredDomainEvent) -> str:
    """Hash every immutable envelope field used for duplicate detection."""
    return canonical_payload_hash(
        {
            "event_id": str(event.event_id),
            "schema_version": event.schema_version,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": str(event.aggregate_id),
            "aggregate_version": event.aggregate_version,
            "project_id": str(event.project_id),
            "project_seq": event.project_seq,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at.isoformat(),
            "payload_hash": event.payload_hash,
            "correlation_id": str(event.correlation_id),
            "causation_id": str(event.causation_id) if event.causation_id else None,
            "actor": event.actor,
            "source_component": event.source_component,
            "artifact_refs": list(event.artifact_refs),
        }
    )


def reduce_execution_plan_event(
    state: ExecutionPlanReplayState | None,
    event: StoredDomainEvent,
) -> ExecutionPlanReplayState:
    """Apply one plan event, rejecting gaps, collisions and corrupt payloads."""
    _validate_event_integrity(event)

    fingerprint = domain_event_fingerprint(event)
    if state is not None and event.event_id in state.seen_event_ids:
        if state.event_fingerprints.get(event.event_id) != fingerprint:
            raise EventIntegrityError("duplicate event ID contains different immutable content")
        return state
    if state is not None:
        if event.project_id != state.project_id or event.aggregate_id != state.aggregate_id:
            raise EventIntegrityError("aggregate identity changed during replay")
        prior_event_id = state.event_ids_by_version.get(event.aggregate_version)
        if prior_event_id is not None and prior_event_id != event.event_id:
            raise EventIntegrityError("aggregate version contains conflicting events")
        expected_version = state.aggregate_version + 1
    else:
        expected_version = 1
    if event.aggregate_version != expected_version:
        raise EventSequenceGap(
            f"expected aggregate version {expected_version}, got {event.aggregate_version}"
        )

    if state is None:
        if event.event_type != ExecutionPlanEventType.PROPOSED:
            raise EventSequenceGap("execution-plan stream must start with proposed")
        raw_plan = event.payload.get("plan")
        if not isinstance(raw_plan, dict):
            raise EventIntegrityError("proposed event is missing the complete plan")
        proposal = ExecutionPlanProposal.model_validate(raw_plan)
        if proposal.id != event.aggregate_id or proposal.project_id != event.project_id:
            raise EventIntegrityError("proposed plan identity differs from event identity")
        if proposal.status is not ExecutionPlanStatus.PROPOSED:
            raise EventIntegrityError("proposed event contains a resolved plan")
        plan = proposal.model_dump(mode="json")
        seen_event_ids: frozenset[UUID] = frozenset({event.event_id})
        ids_by_version = {1: event.event_id}
        fingerprints = {event.event_id: fingerprint}
    else:
        if event.event_type != ExecutionPlanEventType.RESOLVED:
            raise EventIntegrityError("resolved plan stream contains an unsupported transition")
        if str(event.payload.get("execution_plan_id")) != str(state.aggregate_id):
            raise EventIntegrityError("resolved event references another execution plan")
        current = state.proposal
        if event.payload.get("scope_hash") != current.scope_hash:
            raise EventIntegrityError("resolved event scope hash differs from proposed plan")
        try:
            status = ExecutionPlanStatus(str(event.payload.get("status")))
        except ValueError as exc:
            raise EventIntegrityError("resolved event contains an invalid status") from exc
        if status not in {ExecutionPlanStatus.APPROVED, ExecutionPlanStatus.REJECTED}:
            raise EventIntegrityError("resolved event must approve or reject the plan")
        resolved_at = event.payload.get("resolved_at")
        if not isinstance(resolved_at, str):
            raise EventIntegrityError("resolved event is missing resolved_at")
        try:
            resolved_timestamp = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EventIntegrityError("resolved event contains an invalid resolved_at") from exc
        if resolved_timestamp.tzinfo is None:
            raise EventIntegrityError("resolved event resolved_at must include a timezone")
        proposal = current.model_copy(
            update={"status": status, "resolved_at": resolved_timestamp}
        )
        proposal = ExecutionPlanProposal.model_validate(proposal.model_dump(mode="json"))
        plan = proposal.model_dump(mode="json")
        seen_event_ids = frozenset((*state.seen_event_ids, event.event_id))
        ids_by_version = dict(state.event_ids_by_version)
        ids_by_version[event.aggregate_version] = event.event_id
        fingerprints = dict(state.event_fingerprints)
        fingerprints[event.event_id] = fingerprint

    return ExecutionPlanReplayState(
        project_id=event.project_id,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        plan=plan,
        seen_event_ids=seen_event_ids,
        event_ids_by_version=ids_by_version,
        event_fingerprints=fingerprints,
    )


def replay_execution_plan(events: list[StoredDomainEvent]) -> ExecutionPlanReplayState:
    """Rebuild one execution plan from a complete aggregate stream."""
    state: ExecutionPlanReplayState | None = None
    for event in events:
        state = reduce_execution_plan_event(state, event)
    if state is None:
        raise EventSequenceGap("execution-plan stream is empty")
    return state


def replay_execution_plan_from_snapshot(
    snapshot: ExecutionPlanReplaySnapshot,
    tail_events: list[StoredDomainEvent],
) -> ExecutionPlanReplayState:
    """Apply only validated events that occur after an immutable snapshot.

    The caller still owns the from-zero comparison.  This function makes the
    faster path fail closed if a query accidentally includes an old event,
    another aggregate, or a project event recorded before the snapshot cursor.
    """
    state = snapshot.restore_state()
    for event in tail_events:
        if (
            event.project_id != snapshot.project_id
            or event.aggregate_id != snapshot.aggregate_id
            or event.aggregate_type != "execution_plan"
        ):
            raise ReplaySnapshotIntegrityError(
                "replay snapshot tail contains another aggregate"
            )
        if event.project_seq <= snapshot.project_seq:
            raise ReplaySnapshotIntegrityError(
                "replay snapshot tail contains an event at or before its cursor"
            )
        if event.aggregate_version <= snapshot.aggregate_version:
            raise ReplaySnapshotIntegrityError(
                "replay snapshot tail contains an event at or before its version"
            )
        state = reduce_execution_plan_event(state, event)
    return state


def execution_plan_replay_state_hash(state: ExecutionPlanReplayState) -> str:
    """Hash the normalized state used by both full and snapshot-tail replay."""
    return canonical_payload_hash(state.normalized_payload())


__all__ = [
    "DomainEventMetadata",
    "EventUpcaster",
    "EventUpcasterRegistry",
    "EventIntegrityError",
    "EventReplayError",
    "EventSequenceGap",
    "ExecutionPlanEventType",
    "ExecutionPlanReplayState",
    "ExecutionPlanReplaySnapshot",
    "ReplaySnapshotIntegrityError",
    "StoredDomainEvent",
    "UnsupportedEventSchema",
    "canonical_payload_hash",
    "domain_event_fingerprint",
    "execution_plan_replay_state_hash",
    "execution_plan_event_id",
    "reduce_execution_plan_event",
    "replay_execution_plan",
    "replay_execution_plan_from_snapshot",
]
