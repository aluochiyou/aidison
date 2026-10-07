"""Typed, durable requests that a Worker may acknowledge only at a safe point."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class ControlRequestKind(StrEnum):
    PAUSE = "pause"
    RUNTIME_STEERING = "runtime_steering"
    BASIS_STEERING = "basis_steering"


class ControlRequestStatus(StrEnum):
    REQUESTED = "requested"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"


class AgentRunControlRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    agent_run_id: UUID
    kind: ControlRequestKind
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=300)
    status: ControlRequestStatus = ControlRequestStatus.REQUESTED
    created_at: datetime = Field(default_factory=utc_now)
    acknowledged_at: datetime | None = None

    @model_validator(mode="after")
    def state_and_payload_match_kind(self) -> AgentRunControlRequest:
        if self.kind is ControlRequestKind.PAUSE and self.payload:
            raise ValueError("pause request must not carry a free-form payload")
        if self.kind is ControlRequestKind.RUNTIME_STEERING:
            instruction = self.payload.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                raise ValueError("runtime steering requires a non-empty instruction")
        if self.kind is ControlRequestKind.BASIS_STEERING:
            change_request_ref = self.payload.get("change_request_ref")
            if not isinstance(change_request_ref, str) or not change_request_ref.strip():
                raise ValueError("basis steering requires a change_request_ref")
        terminal = self.status in {ControlRequestStatus.ACKNOWLEDGED, ControlRequestStatus.REJECTED}
        if terminal != (self.acknowledged_at is not None):
            raise ValueError("terminal control request status and acknowledged_at must agree")
        return self


__all__ = ["AgentRunControlRequest", "ControlRequestKind", "ControlRequestStatus"]
