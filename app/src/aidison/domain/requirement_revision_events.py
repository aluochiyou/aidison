"""Versioned event authority for immutable approved requirement revisions.

Requirement revisions are a safe first project-domain aggregate to cut over:
they are immutable once approved.  New revisions therefore have one complete
event, while pre-cutover relation rows remain readable as legacy projections.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid5

from pydantic import Field

from aidison.domain.events import (
    DomainEventMetadata,
    EventIntegrityError,
    EventSequenceGap,
    StoredDomainEvent,
    canonical_payload_hash,
    domain_event_fingerprint,
)
from aidison.domain.models import FrozenModel, RequirementRevision, RequirementStatus


class RequirementRevisionEventType(StrEnum):
    APPROVED = "requirements.approved"


_REQUIREMENT_REVISION_EVENT_NAMESPACE = UUID("069fab94-617a-4aed-934b-1ccfc07f39ea")


def requirement_revision_event_id(requirement_id: UUID) -> UUID:
    """Return the stable identity of the one immutable revision event."""

    return uuid5(_REQUIREMENT_REVISION_EVENT_NAMESPACE, str(requirement_id))


def requirement_revision_event_metadata(
    *,
    requirement: RequirementRevision,
    payload: dict[str, object],
) -> DomainEventMetadata:
    """Create a replayable immutable requirement event envelope."""

    return DomainEventMetadata.for_payload(
        event_id=requirement_revision_event_id(requirement.id),
        schema_version=1,
        aggregate_type="requirement_revision",
        aggregate_id=requirement.id,
        aggregate_version=1,
        payload=payload,
        correlation_id=requirement.id,
        actor="application_service",
        source_component="project_application",
        occurred_at=requirement.approved_at or requirement.created_at,
    )


class RequirementRevisionReplayState(FrozenModel):
    """The deterministic state rebuilt from one immutable approval event."""

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    requirement: dict[str, object]
    event_id: UUID
    event_fingerprint: str

    @property
    def value(self) -> RequirementRevision:
        return RequirementRevision.model_validate(self.requirement)

    def normalized_payload(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "requirement": self.requirement,
            "event_id": str(self.event_id),
            "event_fingerprint": self.event_fingerprint,
        }


def reduce_requirement_revision_event(
    state: RequirementRevisionReplayState | None,
    event: StoredDomainEvent,
) -> RequirementRevisionReplayState:
    """Apply exactly one immutable approval event, rejecting any drift."""

    _validate_event(event)
    fingerprint = domain_event_fingerprint(event)
    if state is not None:
        if event.event_id == state.event_id:
            if fingerprint != state.event_fingerprint:
                raise EventIntegrityError("duplicate requirement event changed immutable content")
            return state
        raise EventSequenceGap("requirement revision stream contains more than one event")
    if event.aggregate_version != 1:
        raise EventSequenceGap("requirement revision stream must start at version 1")
    if event.event_type != RequirementRevisionEventType.APPROVED:
        raise EventSequenceGap("requirement revision stream must start with approved")
    raw_requirement = event.payload.get("requirement")
    if not isinstance(raw_requirement, dict):
        raise EventIntegrityError("requirement approval event lacks the complete revision")
    requirement = RequirementRevision.model_validate(raw_requirement)
    if requirement.id != event.aggregate_id or requirement.project_id != event.project_id:
        raise EventIntegrityError("requirement revision identity differs from event aggregate")
    if requirement.status is not RequirementStatus.APPROVED:
        raise EventIntegrityError("requirement event contains a non-approved revision")
    if requirement.approved_at is None:
        raise EventIntegrityError("approved requirement revision lacks approval timestamp")
    return RequirementRevisionReplayState(
        project_id=event.project_id,
        aggregate_id=event.aggregate_id,
        aggregate_version=1,
        requirement=requirement.model_dump(mode="json"),
        event_id=event.event_id,
        event_fingerprint=fingerprint,
    )


def replay_requirement_revision(
    events: list[StoredDomainEvent],
) -> RequirementRevisionReplayState:
    """Rebuild one requirement revision from a complete event stream."""

    state: RequirementRevisionReplayState | None = None
    for event in events:
        state = reduce_requirement_revision_event(state, event)
    if state is None:
        raise EventSequenceGap("requirement revision stream is empty")
    return state


def requirement_revision_replay_state_hash(state: RequirementRevisionReplayState) -> str:
    return canonical_payload_hash(state.normalized_payload())


def _validate_event(event: StoredDomainEvent) -> None:
    if event.schema_version != 1:
        raise EventIntegrityError("unsupported requirement revision event schema")
    if event.aggregate_type != "requirement_revision":
        raise EventIntegrityError("event does not belong to a requirement revision")
    if canonical_payload_hash(event.payload) != event.payload_hash:
        raise EventIntegrityError("requirement revision event payload hash mismatch")


__all__ = [
    "RequirementRevisionEventType",
    "RequirementRevisionReplayState",
    "reduce_requirement_revision_event",
    "replay_requirement_revision",
    "requirement_revision_event_id",
    "requirement_revision_event_metadata",
    "requirement_revision_replay_state_hash",
]
