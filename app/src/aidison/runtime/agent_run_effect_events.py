"""Versioned lifecycle events for authorized external AgentRun effects."""

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
from aidison.runtime.agent_run_effects import AgentRunEffect, AgentRunEffectState


class AgentRunEffectEventType(StrEnum):
    PREPARED = "agent_run_effect.prepared"
    DISPATCHED = "agent_run_effect.dispatched"
    SUCCEEDED = "agent_run_effect.succeeded"
    FAILED = "agent_run_effect.failed"
    AMBIGUOUS = "agent_run_effect.ambiguous"
    RECONCILED_SUCCEEDED = "agent_run_effect.reconciled_succeeded"
    RECONCILED_FAILED = "agent_run_effect.reconciled_failed"


_AGENT_RUN_EFFECT_EVENT_NAMESPACE = UUID("cefc91fd-d409-451d-9e3a-2cfe042f1db5")


def agent_run_effect_event_id(aggregate_id: UUID, aggregate_version: int) -> UUID:
    return uuid5(_AGENT_RUN_EFFECT_EVENT_NAMESPACE, f"{aggregate_id}:{aggregate_version}")


def agent_run_effect_event_metadata(
    *,
    effect: AgentRunEffect,
    aggregate_version: int,
    payload: dict[str, object],
    occurred_at: datetime,
    event_type: AgentRunEffectEventType,
) -> DomainEventMetadata:
    return DomainEventMetadata.for_payload(
        event_id=agent_run_effect_event_id(effect.id, aggregate_version),
        schema_version=1,
        aggregate_type="agent_run_effect",
        aggregate_id=effect.id,
        aggregate_version=aggregate_version,
        payload=payload,
        correlation_id=effect.intent.run_id,
        actor="application_service",
        source_component="agent_run_effect_ledger",
        artifact_refs=_artifact_refs(effect),
        occurred_at=occurred_at,
    )


class AgentRunEffectReplayState(FrozenModel):
    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    effect: dict[str, object]
    seen_event_ids: frozenset[UUID] = Field(default_factory=frozenset)
    event_fingerprints: dict[UUID, str] = Field(default_factory=dict)

    @property
    def agent_run_effect(self) -> AgentRunEffect:
        return AgentRunEffect.model_validate(self.effect)

    def normalized_payload(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "effect": self.effect,
            "seen_event_ids": sorted(str(event_id) for event_id in self.seen_event_ids),
            "event_fingerprints": {
                str(event_id): fingerprint
                for event_id, fingerprint in sorted(
                    self.event_fingerprints.items(), key=lambda item: str(item[0])
                )
            },
        }


_AGENT_RUN_EFFECT_REPLAY_REDUCER_VERSION = "agent-run-effect-replay.v1"


class AgentRunEffectReplaySnapshot(FrozenModel):
    """Immutable accelerator for an effect ledger, including ambiguous outcomes."""

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    project_seq: int = Field(ge=1)
    reducer_version: str = _AGENT_RUN_EFFECT_REPLAY_REDUCER_VERSION
    state: dict[str, object]
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime

    @classmethod
    def from_state(
        cls,
        state: AgentRunEffectReplayState,
        *,
        project_seq: int,
        created_at: datetime,
    ) -> AgentRunEffectReplaySnapshot:
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
    def validates_state_hash_and_identity(self) -> AgentRunEffectReplaySnapshot:
        if self.reducer_version != _AGENT_RUN_EFFECT_REPLAY_REDUCER_VERSION:
            raise ValueError("unsupported AgentRun effect replay reducer version")
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ValueError("AgentRun effect replay snapshot state hash mismatch")
        try:
            state = AgentRunEffectReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ValueError("replay snapshot cannot restore AgentRun effect state") from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ValueError("AgentRun effect snapshot envelope differs from reducer state")
        return self

    def restore_state(self) -> AgentRunEffectReplayState:
        if canonical_payload_hash(self.state) != self.state_hash:
            raise ReplaySnapshotIntegrityError(
                "AgentRun effect replay snapshot state hash mismatch"
            )
        try:
            state = AgentRunEffectReplayState.model_validate(self.state)
        except ValueError as exc:
            raise ReplaySnapshotIntegrityError(
                "replay snapshot cannot restore AgentRun effect state"
            ) from exc
        if (
            state.project_id != self.project_id
            or state.aggregate_id != self.aggregate_id
            or state.aggregate_version != self.aggregate_version
        ):
            raise ReplaySnapshotIntegrityError(
                "AgentRun effect snapshot envelope differs from reducer state"
            )
        return state


def _artifact_refs(effect: AgentRunEffect) -> tuple[str, ...]:
    refs = [effect.intent.request_artifact_ref]
    for optional_ref in (
        effect.response_artifact_ref,
        effect.failure_ref,
        effect.reconciliation_artifact_ref,
    ):
        if optional_ref is not None:
            refs.append(optional_ref)
    return tuple(refs)


def _effect_from_event(event: StoredDomainEvent) -> AgentRunEffect:
    raw_effect = event.payload.get("effect")
    if not isinstance(raw_effect, dict):
        raise EventIntegrityError("AgentRun effect event is missing the complete effect")
    effect = AgentRunEffect.model_validate(raw_effect)
    if effect.id != event.aggregate_id:
        raise EventIntegrityError("AgentRun effect identity differs from event aggregate")
    return effect


def _same_authorization(left: AgentRunEffect, right: AgentRunEffect) -> bool:
    return (
        left.id == right.id
        and left.intent == right.intent
        and left.claim_generation == right.claim_generation
        and left.lease_token == right.lease_token
        and left.created_at == right.created_at
    )


def _validate_transition(
    *,
    previous: AgentRunEffect,
    current: AgentRunEffect,
    event_type: AgentRunEffectEventType,
) -> None:
    if not _same_authorization(previous, current):
        raise EventIntegrityError("AgentRun effect authorization changed during replay")
    if event_type is AgentRunEffectEventType.DISPATCHED:
        if (
            previous.state is not AgentRunEffectState.PREPARED
            or current.state is not AgentRunEffectState.DISPATCHED
            or current.dispatched_at is None
        ):
            raise EventIntegrityError("dispatched event must move a prepared effect to dispatched")
        return
    if event_type is AgentRunEffectEventType.AMBIGUOUS:
        if (
            previous.state is not AgentRunEffectState.DISPATCHED
            or current.state is not AgentRunEffectState.AMBIGUOUS
            or current.normalized_error is None
            or current.resolved_at is None
        ):
            raise EventIntegrityError(
                "ambiguous event must preserve an uncertain dispatched effect"
            )
        return
    if event_type in {AgentRunEffectEventType.SUCCEEDED, AgentRunEffectEventType.FAILED}:
        expected_state = (
            AgentRunEffectState.SUCCEEDED
            if event_type is AgentRunEffectEventType.SUCCEEDED
            else AgentRunEffectState.FAILED
        )
        if previous.state not in {AgentRunEffectState.PREPARED, AgentRunEffectState.DISPATCHED}:
            raise EventIntegrityError("terminal effect follows an already resolved state")
        if current.state is not expected_state or current.resolved_at is None:
            raise EventIntegrityError("terminal effect event has an invalid final state")
        if (
            expected_state is AgentRunEffectState.SUCCEEDED
            and current.response_artifact_ref is None
        ):
            raise EventIntegrityError("successful effect must reference its response artifact")
        if expected_state is AgentRunEffectState.FAILED and current.failure_ref is None:
            raise EventIntegrityError("failed effect must reference its failure artifact")
        return
    expected_reconciled_state = {
        AgentRunEffectEventType.RECONCILED_SUCCEEDED: AgentRunEffectState.SUCCEEDED,
        AgentRunEffectEventType.RECONCILED_FAILED: AgentRunEffectState.FAILED,
    }.get(event_type)
    if expected_reconciled_state is None:
        raise EventIntegrityError("unsupported AgentRun effect lifecycle event")
    if (
        previous.state is not AgentRunEffectState.AMBIGUOUS
        or current.state is not expected_reconciled_state
        or current.reconciliation_artifact_ref is None
        or current.reconciled_at is None
    ):
        raise EventIntegrityError("reconciliation event must resolve an ambiguous effect")


def reduce_agent_run_effect_event(
    state: AgentRunEffectReplayState | None,
    event: StoredDomainEvent,
) -> AgentRunEffectReplayState:
    if event.schema_version != 1:
        raise UnsupportedEventSchema(
            f"AgentRun effect schema {event.schema_version} is not supported"
        )
    if event.aggregate_type != "agent_run_effect":
        raise EventIntegrityError("event does not belong to an AgentRun effect aggregate")
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
                f"AgentRun effect stream must start at version 1, got {event.aggregate_version}"
            )
    else:
        if event.project_id != state.project_id or event.aggregate_id != state.aggregate_id:
            raise EventIntegrityError("AgentRun effect aggregate identity changed during replay")
        if event.aggregate_version != state.aggregate_version + 1:
            raise EventSequenceGap(
                f"expected AgentRun effect version {state.aggregate_version + 1}, "
                f"got {event.aggregate_version}"
            )

    try:
        event_type = AgentRunEffectEventType(event.event_type)
    except ValueError as exc:
        raise EventIntegrityError("unsupported AgentRun effect lifecycle event") from exc
    effect = _effect_from_event(event)
    if state is None:
        if (
            event_type is not AgentRunEffectEventType.PREPARED
            or effect.state is not AgentRunEffectState.PREPARED
        ):
            raise EventIntegrityError("AgentRun effect stream must start with prepared")
    else:
        _validate_transition(
            previous=state.agent_run_effect,
            current=effect,
            event_type=event_type,
        )

    return AgentRunEffectReplayState(
        project_id=event.project_id,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        effect=effect.model_dump(mode="json"),
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


def replay_agent_run_effect(events: list[StoredDomainEvent]) -> AgentRunEffectReplayState:
    state: AgentRunEffectReplayState | None = None
    for event in events:
        state = reduce_agent_run_effect_event(state, event)
    if state is None:
        raise EventSequenceGap("AgentRun effect stream is empty")
    return state


def replay_agent_run_effect_from_snapshot(
    snapshot: AgentRunEffectReplaySnapshot,
    tail_events: list[StoredDomainEvent],
) -> AgentRunEffectReplayState:
    state = snapshot.restore_state()
    for event in tail_events:
        if (
            event.project_id != snapshot.project_id
            or event.aggregate_id != snapshot.aggregate_id
            or event.aggregate_type != "agent_run_effect"
        ):
            raise ReplaySnapshotIntegrityError(
                "AgentRun effect snapshot tail contains another aggregate"
            )
        if event.project_seq <= snapshot.project_seq:
            raise ReplaySnapshotIntegrityError(
                "AgentRun effect snapshot tail contains an event at or before its cursor"
            )
        if event.aggregate_version <= snapshot.aggregate_version:
            raise ReplaySnapshotIntegrityError(
                "AgentRun effect snapshot tail contains an event at or before its version"
            )
        state = reduce_agent_run_effect_event(state, event)
    return state


def agent_run_effect_replay_state_hash(state: AgentRunEffectReplayState) -> str:
    return canonical_payload_hash(state.normalized_payload())


__all__ = [
    "AgentRunEffectEventType",
    "AgentRunEffectReplaySnapshot",
    "AgentRunEffectReplayState",
    "agent_run_effect_event_id",
    "agent_run_effect_event_metadata",
    "agent_run_effect_replay_state_hash",
    "reduce_agent_run_effect_event",
    "replay_agent_run_effect",
    "replay_agent_run_effect_from_snapshot",
]
