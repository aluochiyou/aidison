"""Run-scoped budget contracts for the LangGraph-first execution path.

These contracts deliberately do not reuse the legacy Job/Attempt budget
objects.  A physical external call is accounted for against an AgentRun and
the lease generation that was allowed to start it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentRunBudgetOperationKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"


class AgentRunBudgetState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    SETTLED = "settled"
    RELEASED = "released"
    AMBIGUOUS = "ambiguous"


class AgentRunBudgetAccount(BaseModel):
    """One bounded cost envelope for one AgentRun."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    agent_run_id: UUID
    token_cap: int = Field(ge=0)
    tool_call_cap: int = Field(ge=0)
    token_reserved: int = Field(default=0, ge=0)
    tool_calls_reserved: int = Field(default=0, ge=0)
    token_consumed: int = Field(default=0, ge=0)
    tool_calls_consumed: int = Field(default=0, ge=0)
    state: AgentRunBudgetState = AgentRunBudgetState.OPEN
    created_at: datetime
    closed_at: datetime | None = None

    @model_validator(mode="after")
    def counters_stay_within_cap(self) -> AgentRunBudgetAccount:
        if self.token_reserved + self.token_consumed > self.token_cap:
            raise ValueError("token usage exceeds AgentRun budget cap")
        if self.tool_calls_reserved + self.tool_calls_consumed > self.tool_call_cap:
            raise ValueError("tool-call usage exceeds AgentRun budget cap")
        return self


class AgentRunBudgetOperation(BaseModel):
    """Durable cost record for exactly one physical provider or tool attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    claim_generation: int = Field(ge=1)
    lease_token: UUID
    kind: AgentRunBudgetOperationKind
    logical_step: str = Field(min_length=1, max_length=200)
    physical_attempt_no: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=300)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=200)
    state: AgentRunBudgetState
    reserved_tokens: int = Field(default=0, ge=0)
    reserved_tool_calls: int = Field(default=0, ge=0)
    consumed_tokens: int = Field(default=0, ge=0)
    consumed_tool_calls: int = Field(default=0, ge=0)
    provider_request_id: str | None = Field(default=None, max_length=300)
    request_artifact_ref: str | None = Field(default=None, max_length=500)
    response_artifact_ref: str | None = Field(default=None, max_length=500)
    normalized_error: str | None = None
    created_at: datetime
    dispatched_at: datetime | None = None
    settled_at: datetime | None = None

    @model_validator(mode="after")
    def reservation_shape_matches_kind(self) -> AgentRunBudgetOperation:
        if self.kind is AgentRunBudgetOperationKind.MODEL:
            if self.reserved_tokens < 1 or self.reserved_tool_calls != 0:
                raise ValueError("model operation must reserve tokens and no tool calls")
        elif self.reserved_tokens != 0 or self.reserved_tool_calls != 1:
            raise ValueError("tool operation must reserve one tool call and no tokens")
        if self.consumed_tokens > self.reserved_tokens:
            raise ValueError("consumed tokens cannot exceed reservation")
        if self.consumed_tool_calls > self.reserved_tool_calls:
            raise ValueError("consumed tool calls cannot exceed reservation")
        return self


__all__ = [
    "AgentRunBudgetAccount",
    "AgentRunBudgetOperation",
    "AgentRunBudgetOperationKind",
    "AgentRunBudgetState",
]
