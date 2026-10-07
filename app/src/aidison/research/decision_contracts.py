"""Durable human review contract for one ProposalManifest."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentRunDecisionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AgentRunDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    agent_run_id: UUID
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposal_manifest_ref: str = Field(min_length=1, max_length=500)
    proposal_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: AgentRunDecisionStatus = AgentRunDecisionStatus.PENDING
    answer: str | None = Field(default=None, max_length=40)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> AgentRunDecision:
        if (self.status is AgentRunDecisionStatus.PENDING) != (self.resolved_at is None):
            raise ValueError("pending decision and resolved_at must agree")
        if self.status is AgentRunDecisionStatus.PENDING and self.answer is not None:
            raise ValueError("pending decision cannot have an answer")
        if self.status is not AgentRunDecisionStatus.PENDING and self.answer is None:
            raise ValueError("resolved decision requires answer")
        return self
