"""Typed contracts for the LangGraph-first Research vertical slice.

These objects cross stable boundaries.  They intentionally contain references
and hashes rather than artifact bodies, prompts, private messages, live lease
state, or canonical Project facts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

from aidison.research.strategy import ResearchCollectionPolicy


class ResearchResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class AdmissionDisposition(StrEnum):
    ACCEPTED = "accepted"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class ResearchGraphIdentity(BaseModel):
    """Single-writer identity projection for one bounded Research AgentRun."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis_project_revision: int = Field(ge=1)
    graph_revision: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    state_schema_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    coverage_contract_ref: str = Field(min_length=1, max_length=500)


class TaskEnvelope(BaseModel):
    """Stable task intent; execution ownership is deliberately absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    task_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_revision: int = Field(ge=1)
    capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    input_refs: tuple[str, ...] = Field(max_length=64)
    dependency_task_ids: tuple[UUID, ...] = Field(max_length=32)
    coverage_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    allowed_tool_ids: tuple[str, ...] = Field(max_length=32)
    budget_ref: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=300)
    collection_policy: ResearchCollectionPolicy | None = None

    @model_validator(mode="after")
    def dependency_is_not_self(self) -> TaskEnvelope:
        if self.id in self.dependency_task_ids:
            raise ValueError("TaskEnvelope cannot depend on itself")
        if len(set(self.dependency_task_ids)) != len(self.dependency_task_ids):
            raise ValueError("TaskEnvelope dependencies must be unique")
        return self


class ExecutionGrant(BaseModel):
    """Ephemeral dispatch authority rebuilt from the current AgentRun lease."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    attempt_id: UUID
    generation: int = Field(ge=1)
    lease_token: UUID
    deadline_ref: str = Field(min_length=1, max_length=500)
    idempotency_prefix: str = Field(min_length=1, max_length=300)
    # A scheduler-issued share of the frozen Run budget.  It is ephemeral
    # execution authority, not a second budget ledger: the ledger remains the
    # only source of truth for reserve/settle/ambiguity.
    model_token_cap: int | None = Field(default=None, ge=1)


class ResultEnvelope(BaseModel):
    """Immutable, untrusted producer output; admission is a separate record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    task_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    producer_attempt_id: UUID
    producer_generation: int = Field(ge=1)
    producer_profile_ref: str = Field(min_length=1, max_length=500)
    status: ResearchResultStatus
    artifact_ref: str = Field(min_length=1, max_length=500)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_refs: tuple[str, ...] = Field(max_length=128)
    coverage_observation_refs: tuple[str, ...] = Field(max_length=128)
    unresolved_refs: tuple[str, ...] = Field(max_length=64)
    source_collection_report_ref: str | None = Field(default=None, max_length=500)
    # This is an Artifact reference to the private prompt-selection manifest,
    # not the prompt or its source text.  It lets read-side quality views
    # explain budget-driven omissions without turning GraphState into memory.
    context_manifest_ref: str | None = Field(default=None, max_length=500)
    usage_ref: str | None = Field(default=None, max_length=500)
    failure_ref: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def failure_is_explicit(self) -> ResultEnvelope:
        if self.status is ResearchResultStatus.FAILED and self.failure_ref is None:
            raise ValueError("failed ResultEnvelope requires failure_ref")
        return self


class AdmissionRecord(BaseModel):
    """Control-side verdict; it never mutates producer ResultEnvelope content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    result_id: UUID
    result_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    disposition: AdmissionDisposition
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    admitted_ref: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def accepted_result_has_shared_ref(self) -> AdmissionRecord:
        if self.disposition is AdmissionDisposition.ACCEPTED and self.admitted_ref is None:
            raise ValueError("accepted AdmissionRecord requires admitted_ref")
        if self.disposition is not AdmissionDisposition.ACCEPTED and self.admitted_ref is not None:
            raise ValueError("non-accepted AdmissionRecord cannot expose admitted_ref")
        return self


def validate_result_admission(
    *, result: ResultEnvelope, admission: AdmissionRecord
) -> None:
    """Reject a Control verdict that does not bind this exact producer result.

    This is a small, pure fencing gate.  It does not decide whether research is
    semantically sufficient; that remains the consolidator's responsibility.
    It does ensure a failed producer result never enters the admitted
    dependency projection.
    """

    if admission.run_id != result.run_id:
        raise ValueError("AdmissionRecord run_id does not match ResultEnvelope")
    if admission.result_id != result.id:
        raise ValueError("AdmissionRecord result_id does not match ResultEnvelope")
    if admission.result_manifest_hash != result.manifest_hash:
        raise ValueError("AdmissionRecord manifest hash does not match ResultEnvelope")
    if (
        admission.disposition is AdmissionDisposition.ACCEPTED
        and result.status is ResearchResultStatus.FAILED
    ):
        raise ValueError("failed ResultEnvelope cannot be accepted")


class ProposalManifest(BaseModel):
    """The reviewable proposal reference, not a canonical Project mutation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_ref: str = Field(min_length=1, max_length=500)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AdmittedResultRef(BaseModel):
    """Small state element that has passed Control admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: UUID
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    admitted_ref: str = Field(min_length=1, max_length=500)


class ReducerCollision(BaseModel):
    """Visible evidence that one logical result identity had divergent payloads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: UUID
    variants: tuple[AdmittedResultRef, ...] = Field(min_length=2)


def merge_admitted_result_refs(
    left: tuple[AdmittedResultRef, ...],
    right: tuple[AdmittedResultRef, ...],
) -> tuple[AdmittedResultRef, ...]:
    """Pure set-union reducer with canonical ordering and no silent winner."""

    by_variant = {
        (str(item.result_id), item.manifest_hash, item.admitted_ref): item
        for item in (*left, *right)
    }
    return tuple(
        by_variant[key]
        for key in sorted(by_variant, key=lambda value: (value[0], value[1], value[2]))
    )


def derive_reducer_collisions(
    refs: tuple[AdmittedResultRef, ...],
) -> tuple[ReducerCollision, ...]:
    """Derive, rather than resolve, identity collisions for a later gate."""

    grouped: dict[UUID, list[AdmittedResultRef]] = {}
    for item in refs:
        grouped.setdefault(item.result_id, []).append(item)
    collisions = [
        ReducerCollision(
            result_id=result_id,
            variants=tuple(
                sorted(variants, key=lambda item: (item.manifest_hash, item.admitted_ref))
            ),
        )
        for result_id, variants in grouped.items()
        if len(variants) > 1
    ]
    return tuple(sorted(collisions, key=lambda item: str(item.result_id)))


class ResearchGraphState(TypedDict, total=False):
    """Root state is a compact, admitted-reference projection only."""

    identity: ResearchGraphIdentity
    plan_revision_ref: str
    task_envelope_refs: tuple[str, ...]
    admitted_result_refs: Annotated[tuple[AdmittedResultRef, ...], merge_admitted_result_refs]
    admission_record_refs: tuple[str, ...]
    coverage_projection_ref: str
    conflict_projection_ref: str
    sufficiency_outcome_ref: str
    pending_interrupt_ref: str
    usage_ledger_refs: tuple[str, ...]
    proposal_manifest_ref: str


__all__ = [
    "AdmissionDisposition",
    "AdmissionRecord",
    "AdmittedResultRef",
    "ExecutionGrant",
    "ProposalManifest",
    "ReducerCollision",
    "ResearchGraphIdentity",
    "ResearchGraphState",
    "ResearchResultStatus",
    "ResultEnvelope",
    "TaskEnvelope",
    "derive_reducer_collisions",
    "merge_admitted_result_refs",
    "validate_result_admission",
]
