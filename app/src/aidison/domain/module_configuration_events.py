"""Versioned lifecycle events for immutable module configuration revisions."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid5

from aidison.domain.events import (
    DomainEventMetadata,
    EventIntegrityError,
    EventSequenceGap,
    StoredDomainEvent,
    canonical_payload_hash,
)
from aidison.domain.models import FrozenModel, ModuleConfiguration, ModuleConfigurationStatus


class ModuleConfigurationEventType(StrEnum):
    CREATED = "module_configuration.created"
    SUPERSEDED = "module_configuration.superseded"


_NAMESPACE = UUID("492af6e2-ee29-4fc7-b581-4c3bd5053136")


def _event_id(configuration_id: UUID, event_type: ModuleConfigurationEventType) -> UUID:
    return uuid5(_NAMESPACE, f"{configuration_id}:{event_type.value}")


def module_configuration_event_metadata(
    *,
    configuration: ModuleConfiguration,
    event_type: ModuleConfigurationEventType,
    payload: dict[str, object],
) -> DomainEventMetadata:
    return DomainEventMetadata.for_payload(
        event_id=_event_id(configuration.id, event_type),
        schema_version=1,
        aggregate_type="module_configuration",
        aggregate_id=configuration.id,
        aggregate_version=1 if event_type is ModuleConfigurationEventType.CREATED else 2,
        payload=payload,
        correlation_id=configuration.id,
        actor="application_service",
        source_component="project_application",
        occurred_at=configuration.created_at,
    )


class ModuleConfigurationReplayState(FrozenModel):
    project_id: UUID
    aggregate_id: UUID
    aggregate_version: int
    configuration: dict[str, object]

    @property
    def value(self) -> ModuleConfiguration:
        return ModuleConfiguration.model_validate(self.configuration)


def replay_module_configuration(
    events: list[StoredDomainEvent],
) -> ModuleConfigurationReplayState:
    state: ModuleConfigurationReplayState | None = None
    for event in events:
        _validate_event(event)
        raw = event.payload.get("configuration")
        if not isinstance(raw, dict):
            raise EventIntegrityError("module configuration event lacks complete configuration")
        configuration = ModuleConfiguration.model_validate(raw)
        if configuration.id != event.aggregate_id or configuration.project_id != event.project_id:
            raise EventIntegrityError("module configuration identity differs from event aggregate")
        if state is None:
            if (
                event.aggregate_version != 1
                or event.event_type != ModuleConfigurationEventType.CREATED
            ):
                raise EventSequenceGap("configuration stream must start with creation")
            if configuration.status is not ModuleConfigurationStatus.ACTIVE:
                raise EventIntegrityError("created configuration must be active")
            state = ModuleConfigurationReplayState(
                project_id=event.project_id,
                aggregate_id=event.aggregate_id,
                aggregate_version=1,
                configuration=configuration.model_dump(mode="json"),
            )
            continue
        if event.aggregate_version != state.aggregate_version + 1:
            raise EventSequenceGap("module configuration aggregate version is not contiguous")
        if event.event_type != ModuleConfigurationEventType.SUPERSEDED:
            raise EventSequenceGap("only supersede may follow configuration creation")
        current = state.value
        if (
            current.status is not ModuleConfigurationStatus.ACTIVE
            or configuration.status is not ModuleConfigurationStatus.SUPERSEDED
        ):
            raise EventIntegrityError(
                "configuration supersession must transition active to superseded"
            )
        if configuration.model_copy(
            update={"status": ModuleConfigurationStatus.ACTIVE, "superseded_by_id": None}
        ) != current:
            raise EventIntegrityError("configuration supersession changed immutable options")
        state = ModuleConfigurationReplayState(
            project_id=event.project_id,
            aggregate_id=event.aggregate_id,
            aggregate_version=event.aggregate_version,
            configuration=configuration.model_dump(mode="json"),
        )
    if state is None:
        raise EventSequenceGap("module configuration stream is empty")
    return state


def _validate_event(event: StoredDomainEvent) -> None:
    if event.schema_version != 1 or event.aggregate_type != "module_configuration":
        raise EventIntegrityError("event does not belong to a module configuration")
    if canonical_payload_hash(event.payload) != event.payload_hash:
        raise EventIntegrityError("module configuration event payload hash mismatch")


__all__ = [
    "ModuleConfigurationEventType",
    "ModuleConfigurationReplayState",
    "module_configuration_event_metadata",
    "replay_module_configuration",
]
