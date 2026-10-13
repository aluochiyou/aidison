"""Event authority for one approved project/module structure version.

``ProjectBlueprint`` is the bounded aggregate that owns a confirmed module
set, its execution dependencies and its engineering couplings.  The module
rows remain indexed read projections; the event stream is the authoritative
record of which immutable structure was approved and, later, superseded.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid5

from aidison.domain.events import (
    DomainEventMetadata,
    EventIntegrityError,
    EventSequenceGap,
    StoredDomainEvent,
    canonical_payload_hash,
    domain_event_fingerprint,
)
from aidison.domain.models import BlueprintStatus, FrozenModel, ProjectBlueprint


class ProjectBlueprintEventType(StrEnum):
    CREATED = "blueprint.created"
    SUPERSEDED = "blueprint.superseded"


_PROJECT_BLUEPRINT_EVENT_NAMESPACE = UUID("95a7da30-a06f-4e93-a624-bfbc7d903045")


def project_blueprint_event_id(
    blueprint_id: UUID,
    event_type: ProjectBlueprintEventType,
) -> UUID:
    """Return an idempotent identity for one immutable blueprint transition."""

    return uuid5(_PROJECT_BLUEPRINT_EVENT_NAMESPACE, f"{blueprint_id}:{event_type.value}")


def project_blueprint_event_metadata(
    *,
    blueprint: ProjectBlueprint,
    event_type: ProjectBlueprintEventType,
    payload: dict[str, object],
) -> DomainEventMetadata:
    """Wrap a full structural snapshot in a versioned event envelope."""

    aggregate_version = (
        1 if event_type is ProjectBlueprintEventType.CREATED else 2
    )
    return DomainEventMetadata.for_payload(
        event_id=project_blueprint_event_id(blueprint.id, event_type),
        schema_version=1,
        aggregate_type="project_blueprint",
        aggregate_id=blueprint.id,
        aggregate_version=aggregate_version,
        payload=payload,
        correlation_id=blueprint.id,
        actor="application_service",
        source_component="project_application",
        occurred_at=blueprint.created_at,
    )


class ProjectBlueprintReplayState(FrozenModel):
    """Complete current state rebuilt from a blueprint's own event stream."""

    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int
    blueprint: dict[str, object]
    event_fingerprints: tuple[str, ...]

    @property
    def value(self) -> ProjectBlueprint:
        return ProjectBlueprint.model_validate(self.blueprint)

    def normalized_payload(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version,
            "blueprint": self.blueprint,
            "event_fingerprints": self.event_fingerprints,
        }


def reduce_project_blueprint_event(
    state: ProjectBlueprintReplayState | None,
    event: StoredDomainEvent,
) -> ProjectBlueprintReplayState:
    """Apply only legal immutable structure lifecycle transitions."""

    _validate_event(event)
    fingerprint = domain_event_fingerprint(event)
    raw_blueprint = event.payload.get("blueprint")
    if not isinstance(raw_blueprint, dict):
        raise EventIntegrityError("blueprint event lacks the complete structural snapshot")
    blueprint = ProjectBlueprint.model_validate(raw_blueprint)
    if blueprint.id != event.aggregate_id or blueprint.project_id != event.project_id:
        raise EventIntegrityError("project blueprint identity differs from event aggregate")

    if state is None:
        if event.aggregate_version != 1 or event.event_type != ProjectBlueprintEventType.CREATED:
            raise EventSequenceGap("blueprint stream must start with a creation event")
        if blueprint.status is not BlueprintStatus.ACTIVE:
            raise EventIntegrityError("created project blueprint must be active")
        return ProjectBlueprintReplayState(
            project_id=event.project_id,
            aggregate_id=event.aggregate_id,
            aggregate_version=1,
            blueprint=blueprint.model_dump(mode="json"),
            event_fingerprints=(fingerprint,),
        )

    if event.aggregate_version != state.aggregate_version + 1:
        raise EventSequenceGap("project blueprint aggregate version is not contiguous")
    if event.event_type != ProjectBlueprintEventType.SUPERSEDED:
        raise EventSequenceGap("only supersede may follow project blueprint creation")
    current = state.value
    if (
        current.status is not BlueprintStatus.ACTIVE
        or blueprint.status is not BlueprintStatus.SUPERSEDED
    ):
        raise EventIntegrityError("blueprint supersession must transition active to superseded")
    if blueprint.model_copy(update={"status": BlueprintStatus.ACTIVE}) != current:
        raise EventIntegrityError("blueprint supersession changed immutable structure")
    return ProjectBlueprintReplayState(
        project_id=event.project_id,
        aggregate_id=event.aggregate_id,
        aggregate_version=event.aggregate_version,
        blueprint=blueprint.model_dump(mode="json"),
        event_fingerprints=(*state.event_fingerprints, fingerprint),
    )


def replay_project_blueprint(
    events: list[StoredDomainEvent],
) -> ProjectBlueprintReplayState:
    """Rebuild one project structure from its complete event history."""

    state: ProjectBlueprintReplayState | None = None
    for event in events:
        state = reduce_project_blueprint_event(state, event)
    if state is None:
        raise EventSequenceGap("project blueprint stream is empty")
    return state


def project_blueprint_replay_state_hash(state: ProjectBlueprintReplayState) -> str:
    return canonical_payload_hash(state.normalized_payload())


def _validate_event(event: StoredDomainEvent) -> None:
    if event.schema_version != 1:
        raise EventIntegrityError("unsupported project blueprint event schema")
    if event.aggregate_type != "project_blueprint":
        raise EventIntegrityError("event does not belong to a project blueprint")
    if canonical_payload_hash(event.payload) != event.payload_hash:
        raise EventIntegrityError("project blueprint event payload hash mismatch")


__all__ = [
    "ProjectBlueprintEventType",
    "ProjectBlueprintReplayState",
    "project_blueprint_event_id",
    "project_blueprint_event_metadata",
    "project_blueprint_replay_state_hash",
    "reduce_project_blueprint_event",
    "replay_project_blueprint",
]
