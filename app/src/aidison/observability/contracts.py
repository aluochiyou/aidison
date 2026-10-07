"""Observation data contracts and the adapter to the existing durable event stream.

These models deliberately describe observability data only.  They cannot own
business state, grant authority, move a Run, or replace the PostgreSQL event
sequence that already powers durable product-event replay.
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from aidison.evaluation.contracts import FrozenModel


class DurableProductEventType(StrEnum):
    RUN_STARTED = "run.started"
    PLAN_ADMITTED = "plan.admitted"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    RESULT_ADMITTED = "result.admitted"
    RESULT_QUARANTINED = "result.quarantined"
    COVERAGE_CHANGED = "coverage.changed"
    CONFLICT_DETECTED = "conflict.detected"
    CONFLICT_RESOLVED = "conflict.resolved"
    VERIFICATION_FINDING = "verification.finding"
    DECISION_REQUESTED = "decision.requested"
    DECISION_RESOLVED = "decision.resolved"
    RUN_WAITING = "run.waiting"
    RUN_PARTIAL = "run.partial"
    RUN_COMPLETED = "run.completed"
    RUN_BLOCKED = "run.blocked"
    PROJECT_REVISION_CREATED = "project.revision.created"


class EphemeralLiveEventType(StrEnum):
    TOKEN_DELTA = "token.delta"
    TOOL_STAGE = "tool.stage"
    PROGRESS_HINT = "progress.hint"
    MODEL_STATE = "model.state"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class TelemetryCorrelation(FrozenModel):
    """Join keys shared by product events, logs, metrics and traces."""

    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: UUID | None = None
    run_id: UUID | None = None
    task_id: str | None = Field(default=None, min_length=1, max_length=300)
    invocation_id: UUID | None = None
    artifact_ref: str | None = Field(default=None, min_length=1, max_length=500)
    decision_request_id: UUID | None = None


class DurableProductEvent(FrozenModel):
    """User/recovery-relevant event that belongs in PostgreSQL's append-only stream."""

    project_id: UUID
    event_type: DurableProductEventType
    correlation: TelemetryCorrelation = Field(default_factory=TelemetryCorrelation)
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def payload_excludes_sensitive_and_private_content(
        cls, value: dict[str, object]
    ) -> dict[str, object]:
        _assert_safe_attributes(value)
        return value

    @model_validator(mode="after")
    def correlation_matches_event_project(self) -> DurableProductEvent:
        if (
            self.correlation.project_id is not None
            and self.correlation.project_id != self.project_id
        ):
            raise ValueError("durable event correlation project_id must equal event project_id")
        return self


class EphemeralLiveEvent(FrozenModel):
    """Loss-tolerant UI event; it deliberately carries no durable cursor or sequence."""

    event_type: EphemeralLiveEventType
    correlation: TelemetryCorrelation = Field(default_factory=TelemetryCorrelation)
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def payload_excludes_sensitive_and_private_content(
        cls, value: dict[str, object]
    ) -> dict[str, object]:
        _assert_safe_attributes(value)
        return value


class StructuredLogRecord(FrozenModel):
    """Developer-facing diagnosis record; safe to serialize to any log sink."""

    timestamp: datetime
    level: LogLevel
    service: str = Field(min_length=1, max_length=100)
    event_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,119}$")
    correlation: TelemetryCorrelation = Field(default_factory=TelemetryCorrelation)
    graph_revision: str | None = Field(default=None, min_length=1, max_length=200)
    failure_class: str | None = Field(default=None, min_length=1, max_length=100)
    duration_ms: int | None = Field(default=None, ge=0)
    attributes: dict[str, object] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def attributes_exclude_sensitive_and_private_content(
        cls, value: dict[str, object]
    ) -> dict[str, object]:
        _assert_safe_attributes(value)
        return value


class MetricPoint(FrozenModel):
    """Export-ready scalar metric.  It is not a durable business event."""

    name: str = Field(pattern=r"^aidison_[a-z][a-z0-9_]{1,119}$")
    value: float
    correlation: TelemetryCorrelation = Field(default_factory=TelemetryCorrelation)
    dimensions: dict[str, str] = Field(default_factory=dict)

    @field_validator("dimensions")
    @classmethod
    def dimensions_exclude_sensitive_and_private_content(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        _assert_safe_attributes(value)
        return value


class ProjectEventAppender(Protocol):
    def append_event(
        self,
        project_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> Awaitable[int]: ...


async def append_durable_product_event(
    appender: ProjectEventAppender,
    event: DurableProductEvent,
) -> int:
    """Append a typed product event through the existing transaction-bound store port."""
    payload = dict(event.payload)
    payload["telemetry"] = event.correlation.model_dump(mode="json", exclude_none=True)
    return await appender.append_event(event.project_id, event.event_type.value, payload)


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "password",
    "secret",
    "authorization",
    "credential",
    "raw_prompt",
    "raw_response",
    "hidden_reasoning",
    "chain_of_thought",
)


def _assert_safe_attributes(value: object, *, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(f"{path}.{key} contains sensitive or private content")
            _assert_safe_attributes(nested, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_safe_attributes(nested, path=f"{path}[{index}]")
        return
    if not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError(f"{path} must be JSON-safe")


__all__ = [
    "DurableProductEvent",
    "DurableProductEventType",
    "EphemeralLiveEvent",
    "EphemeralLiveEventType",
    "LogLevel",
    "MetricPoint",
    "ProjectEventAppender",
    "StructuredLogRecord",
    "TelemetryCorrelation",
    "append_durable_product_event",
]
