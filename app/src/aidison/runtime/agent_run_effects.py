"""Durable external-write contracts for the LangGraph AgentRun path."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class AgentRunEffectState(StrEnum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class EffectReconciliationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentRunEffectIntent(BaseModel):
    """One already-authorized external write; execution is separately recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    task_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_ref: str = Field(pattern=r"^effect-approval://.+", max_length=500)
    effect_kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,99}$")
    provider: str = Field(min_length=1, max_length=100)
    external_idempotency_key: str = Field(min_length=1, max_length=300)
    idempotency_key: str = Field(min_length=1, max_length=300)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_artifact_ref: str = Field(min_length=1, max_length=500)


class AgentRunEffect(BaseModel):
    """Control fact for one external effect; never a provider response body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    intent: AgentRunEffectIntent
    claim_generation: int = Field(ge=1)
    lease_token: UUID
    state: AgentRunEffectState
    provider_effect_id: str | None = Field(default=None, max_length=300)
    response_artifact_ref: str | None = Field(default=None, max_length=500)
    failure_ref: str | None = Field(default=None, max_length=500)
    normalized_error: str | None = Field(default=None, max_length=1_000)
    reconciliation_artifact_ref: str | None = Field(default=None, max_length=500)
    created_at: datetime
    dispatched_at: datetime | None = None
    resolved_at: datetime | None = None
    reconciled_at: datetime | None = None


__all__ = [
    "AgentRunEffect",
    "AgentRunEffectIntent",
    "AgentRunEffectState",
    "EffectReconciliationOutcome",
]
