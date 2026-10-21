from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aidison.domain.events import EventIntegrityError, StoredDomainEvent
from aidison.domain.models import ModuleConfiguration, ModuleConfigurationStatus
from aidison.domain.module_configuration_events import (
    ModuleConfigurationEventType,
    module_configuration_event_metadata,
    replay_module_configuration,
)


def _configuration(**updates: object) -> ModuleConfiguration:
    values: dict[str, object] = {
        "project_id": uuid4(),
        "module_id": uuid4(),
        "revision": 1,
        "options": {"selected_candidate_id": "fc-a"},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(updates)
    return ModuleConfiguration(**values)


def _event(
    configuration: ModuleConfiguration,
    event_type: ModuleConfigurationEventType,
) -> StoredDomainEvent:
    payload: dict[str, object] = {"configuration": configuration.model_dump(mode="json")}
    metadata = module_configuration_event_metadata(
        configuration=configuration,
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
        project_id=configuration.project_id,
        project_seq=metadata.aggregate_version,
        event_type=event_type.value,
        payload=payload,
        recorded_at=metadata.occurred_at,
    )


def test_configuration_replay_preserves_options_through_supersession() -> None:
    active = _configuration()
    superseded = active.model_copy(
        update={"status": ModuleConfigurationStatus.SUPERSEDED, "superseded_by_id": uuid4()}
    )

    state = replay_module_configuration(
        [
            _event(active, ModuleConfigurationEventType.CREATED),
            _event(superseded, ModuleConfigurationEventType.SUPERSEDED),
        ]
    )

    assert state.value == superseded


def test_configuration_replay_rejects_changed_options_during_supersession() -> None:
    active = _configuration()
    altered = active.model_copy(
        update={
            "status": ModuleConfigurationStatus.SUPERSEDED,
            "superseded_by_id": uuid4(),
            "options": {"selected_candidate_id": "fc-b"},
        }
    )

    with pytest.raises(EventIntegrityError, match="immutable options"):
        replay_module_configuration(
            [
                _event(active, ModuleConfigurationEventType.CREATED),
                _event(altered, ModuleConfigurationEventType.SUPERSEDED),
            ]
        )
