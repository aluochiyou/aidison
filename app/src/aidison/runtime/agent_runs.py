"""Thin PostgreSQL control contracts for LangGraph-owned AgentRun execution."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.runtime.identity import RuntimeBinding


def utc_now() -> datetime:
    return datetime.now(UTC)


class AgentRunKind(StrEnum):
    RESEARCH = "research"
    SOLUTION = "solution"
    IMPACT = "impact"
    PROJECT_VERIFICATION = "project_verification"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AdmittedCheckpointRef(BaseModel):
    """Control-owned recovery anchor, not LangGraph's physical latest checkpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str = Field(min_length=1, max_length=300)
    checkpoint_ns: str = Field(default="", max_length=500)
    checkpoint_id: str = Field(min_length=1, max_length=300)
    graph_revision: str = Field(min_length=1, max_length=120)
    state_schema_version: str = Field(min_length=1, max_length=120)
    generation: int = Field(ge=1)
    # Captured by the Control plane when an event-backed run admits the
    # checkpoint. ``0`` keeps historical anchors readable but marks them as
    # insufficient for deterministic E3 replay.
    event_cursor: int = Field(default=0, ge=0)
    invocation_recording_keys: tuple[str, ...] = Field(default=(), max_length=512)

    @model_validator(mode="after")
    def invocation_recording_keys_are_unique(self) -> AdmittedCheckpointRef:
        if len(set(self.invocation_recording_keys)) != len(self.invocation_recording_keys):
            raise ValueError("checkpoint invocation recording keys must be unique")
        return self


class AgentRun(BaseModel):
    """A bounded execution with one immutable runtime authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    kind: AgentRunKind
    idempotency_key: str = Field(min_length=1, max_length=300)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis_project_revision: int = Field(ge=1)
    runtime_binding: RuntimeBinding
    thread_id: str = Field(min_length=1, max_length=300)
    run_contract_ref: str | None = Field(default=None, max_length=500)
    coverage_contract_ref: str | None = Field(default=None, max_length=500)
    status: AgentRunStatus = AgentRunStatus.QUEUED
    cancel_requested: bool = False
    current_generation: int = Field(default=0, ge=0)
    admitted_checkpoint: AdmittedCheckpointRef | None = None
    created_at: datetime = Field(default_factory=utc_now)
    # Set once when a worker first claims the Run and preserved across a
    # lease recovery. Queue time must not silently consume the plan duration.
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def terminal_state_is_consistent(self) -> AgentRun:
        terminal = self.status in {
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
        if terminal != (self.completed_at is not None):
            raise ValueError("terminal AgentRun status and completed_at must agree")
        return self


class AgentRunClaim(BaseModel):
    """Short-lived execution ownership reconstructed into ExecutionGrant later."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    worker_id: str = Field(min_length=1, max_length=200)
    generation: int = Field(ge=1)
    lease_token: UUID
    lease_expires_at: datetime
