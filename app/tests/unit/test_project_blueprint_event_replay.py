from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aidison.domain.events import EventIntegrityError, EventSequenceGap, StoredDomainEvent
from aidison.domain.models import BlueprintStatus, ProjectBlueprint
from aidison.domain.project_blueprint_events import (
    ProjectBlueprintEventType,
    project_blueprint_event_metadata,
    replay_project_blueprint,
)


def _blueprint(**updates: object) -> ProjectBlueprint:
    project_id = uuid4()
    module_id = uuid4()
    values: dict[str, object] = {
        "project_id": project_id,
        "requirement_revision_id": uuid4(),
        "version": 1,
        "module_ids": (module_id,),
        "dependency_edges": (),
        "status": BlueprintStatus.ACTIVE,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(updates)
    return ProjectBlueprint(**values)


def _event(
    blueprint: ProjectBlueprint,
    event_type: ProjectBlueprintEventType,
) -> StoredDomainEvent:
    payload: dict[str, object] = {"blueprint": blueprint.model_dump(mode="json")}
    metadata = project_blueprint_event_metadata(
        blueprint=blueprint,
        event_type=event_type,
        payload=payload,
    )
    return StoredDomainEvent(
        event_id=metadata.event_id,
        schema_version=metadata.schema_version,
        aggregate_type=metadata.aggregate_type,
        aggregate_id=metadata.aggregate_id,
        aggregate_version=metadata.aggregate_version,
        occurred_at=metadata.occurred_at,
        payload_hash=metadata.payload_hash,
        correlation_id=metadata.correlation_id,
        causation_id=metadata.causation_id,
        actor=metadata.actor,
        source_component=metadata.source_component,
        artifact_refs=metadata.artifact_refs,
        project_id=blueprint.project_id,
        project_seq=metadata.aggregate_version,
        event_type=event_type.value,
        payload=payload,
        recorded_at=metadata.occurred_at,
    )


def test_blueprint_events_rebuild_the_approved_structure_then_supersession() -> None:
    active = _blueprint()
    superseded = active.model_copy(update={"status": BlueprintStatus.SUPERSEDED})

    state = replay_project_blueprint(
        [
            _event(active, ProjectBlueprintEventType.CREATED),
            _event(superseded, ProjectBlueprintEventType.SUPERSEDED),
        ]
    )

    assert state.value == superseded
    assert state.aggregate_version == 2


def test_blueprint_replay_rejects_structural_mutation_during_supersession() -> None:
    active = _blueprint()
    altered = active.model_copy(
        update={"status": BlueprintStatus.SUPERSEDED, "module_ids": (uuid4(),)}
    )

    with pytest.raises(EventIntegrityError, match="immutable structure"):
        replay_project_blueprint(
            [
                _event(active, ProjectBlueprintEventType.CREATED),
                _event(altered, ProjectBlueprintEventType.SUPERSEDED),
            ]
        )


def test_blueprint_replay_rejects_orphaned_supersession() -> None:
    superseded = _blueprint(status=BlueprintStatus.SUPERSEDED)

    with pytest.raises(EventSequenceGap, match="start with a creation"):
        replay_project_blueprint([_event(superseded, ProjectBlueprintEventType.SUPERSEDED)])
