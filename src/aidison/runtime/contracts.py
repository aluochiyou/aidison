from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_DELEGATION_WAVE_SIZE = 8


class RuntimeContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DelegationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class JoinStatus(StrEnum):
    OPEN = "open"
    JOINED = "joined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class ResultDisposition(StrEnum):
    ELIGIBLE = "eligible"
    QUARANTINED = "quarantined"


class BudgetOwnerKind(StrEnum):
    PARENT = "parent"
    CHILD = "child"
    JOIN = "join"
    AUDIT = "audit"


class BudgetOperationKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"


class BudgetOperationState(StrEnum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    SETTLED = "settled"
    RELEASED = "released"
    AMBIGUOUS = "ambiguous"


class JoinMode(StrEnum):
    ALL_REQUIRED = "all_required"
    BOUNDED_PARTIAL = "bounded_partial"


class AgentProfileRevision(RuntimeContract):
    profile_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)
    purpose: str = Field(min_length=1, max_length=1_000)
    prompt_template: str = Field(min_length=1, max_length=100_000)
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_schema_ref: str = Field(min_length=1, max_length=500)
    output_schema_ref: str = Field(min_length=1, max_length=500)
    allowed_tool_classes: tuple[str, ...] = ()
    allowed_effects: tuple[str, ...] = ()
    memory_read_scopes: tuple[str, ...] = ()
    memory_write_scopes: tuple[str, ...] = ()
    model_capabilities: tuple[str, ...] = ()
    token_cap: int = Field(gt=0)
    tool_call_cap: int = Field(ge=0)
    concurrency_cap: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    evaluator_policy: dict[str, Any] = Field(default_factory=dict)


class ProfileBinding(RuntimeContract):
    root_job_id: UUID
    role_key: str = Field(min_length=1, max_length=200)
    profile_id: str = Field(min_length=1, max_length=200)
    profile_revision: int = Field(ge=1)
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class BudgetAllocation(RuntimeContract):
    allocation_id: UUID
    account_id: UUID
    owner_kind: BudgetOwnerKind
    owner_ref: UUID
    token_grant: int = Field(ge=0)
    tool_call_grant: int = Field(ge=0)
    token_reserved: int = Field(ge=0)
    tool_calls_reserved: int = Field(ge=0)
    token_consumed: int = Field(ge=0)
    tool_calls_consumed: int = Field(ge=0)
    status: str


class BudgetOperation(RuntimeContract):
    operation_id: UUID
    allocation_id: UUID
    attempt_id: UUID
    claim_generation: int = Field(ge=1)
    lease_token: UUID
    kind: BudgetOperationKind
    state: BudgetOperationState
    idempotency_key: str = Field(min_length=1, max_length=300)
    reserved_tokens: int = Field(ge=0)
    reserved_tool_calls: int = Field(ge=0)
    consumed_tokens: int = Field(ge=0)
    consumed_tool_calls: int = Field(ge=0)
    provider_request_id: str | None = None


class DelegationSpec(RuntimeContract):
    delegation_id: UUID = Field(default_factory=uuid4)
    parent_job_id: UUID
    parent_attempt_id: UUID
    parent_claim_generation: int = Field(ge=1)
    graph_step_id: str = Field(min_length=1, max_length=200)
    task_kind: str = Field(default="research", pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    profile_id: str = Field(min_length=1, max_length=200)
    profile_revision: int = Field(ge=1)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    shard_key: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=300)
    input_refs: tuple[str, ...] = ()
    allowed_effects: tuple[str, ...] = ("discovery", "read")
    token_budget: int = Field(gt=0)
    tool_call_budget: int = Field(ge=0)
    deadline: datetime
    canonical_write: bool = False

    @model_validator(mode="after")
    def enforce_v0_write_boundary(self) -> DelegationSpec:
        if self.canonical_write:
            raise ValueError("V0 delegation cannot write canonical state")
        return self


class DelegationResult(RuntimeContract):
    delegation_id: UUID
    child_job_id: UUID
    attempt_id: UUID
    child_claim_generation: int = Field(ge=1)
    status: DelegationStatus
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposal_ref: str | None = None
    evidence_candidate_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    warnings: tuple[str, ...] = ()
    normalized_error: str | None = None


class JoinPolicy(RuntimeContract):
    mode: JoinMode
    expected_delegation_ids: tuple[UUID, ...] = Field(
        min_length=1,
        max_length=MAX_DELEGATION_WAVE_SIZE,
    )
    min_successes: int = Field(ge=1, le=MAX_DELEGATION_WAVE_SIZE)
    deadline: datetime

    @model_validator(mode="after")
    def validate_success_threshold(self) -> JoinPolicy:
        if self.min_successes > len(self.expected_delegation_ids):
            raise ValueError("min_successes exceeds expected delegations")
        if self.mode is JoinMode.ALL_REQUIRED and self.min_successes != len(
            self.expected_delegation_ids
        ):
            raise ValueError("all_required needs every delegation")
        return self


class RejectedResult(RuntimeContract):
    result_ref: str
    reason: str


class JobClaim(RuntimeContract):
    job_id: UUID
    attempt_id: UUID
    attempt_number: int = Field(ge=1)
    claim_generation: int = Field(ge=1)
    lease_token: UUID
    lease_owner: str = Field(min_length=1, max_length=200)
    lease_expires_at: datetime
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis_project_revision: int = Field(ge=1)
    profile_id: str = Field(min_length=1, max_length=200)
    profile_revision: int = Field(ge=1)


class RuntimeWorkItem(RuntimeContract):
    claim: JobClaim
    project_id: UUID
    kind: str = Field(min_length=1, max_length=100)
    request_payload: dict[str, Any] = Field(default_factory=dict)
    delegation: DelegationSpec | None = None


class JoinSnapshot(RuntimeContract):
    join_group_id: UUID
    status: JoinStatus
    ready: bool
    impossible: bool
    accepted_proposal_refs: tuple[str, ...] = ()
    rejected_results: tuple[RejectedResult, ...] = ()


class DelegationWave(RuntimeContract):
    join_group_id: UUID
    delegation_ids: tuple[UUID, ...] = Field(
        min_length=1,
        max_length=MAX_DELEGATION_WAVE_SIZE,
    )
    child_job_ids: tuple[UUID, ...] = Field(
        min_length=1,
        max_length=MAX_DELEGATION_WAVE_SIZE,
    )

    @model_validator(mode="after")
    def sizes_match(self) -> DelegationWave:
        if len(self.delegation_ids) != len(self.child_job_ids):
            raise ValueError("each delegation must own one child job")
        return self


class RegisteredResult(RuntimeContract):
    result_id: UUID
    result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    disposition: ResultDisposition
    quarantine_reason: str | None = None


class JoinReceipt(RuntimeContract):
    join_group_id: UUID
    accepted_result_hashes: tuple[str, ...]
    rejected_results: tuple[RejectedResult, ...] = ()
    merged_proposal_ref: str
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_claim_generation: int = Field(ge=1)
    committed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CommittedJoin(RuntimeContract):
    """A terminal join that a reclaimed parent can safely resume."""

    receipt: JoinReceipt
    input_refs: tuple[str, ...] = ()
