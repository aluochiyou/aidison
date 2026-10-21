"""Stable, ref-only contracts for the SolutionGraph vertical.

SolutionGraph deliberately reuses the runtime task, execution-grant, result,
and admission contracts from ResearchGraph.  This module adds only the
solution-specific frozen input contract and compact root-state identity; it
does not introduce a second scheduler or a second canonical fact store.
"""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

from aidison.research.langgraph_contracts import (
    AdmittedResultRef,
    merge_admitted_result_refs,
)


class SolutionRiskClass(StrEnum):
    """Risk level that selects the frozen verification policy, not an LLM score."""

    STANDARD = "standard"
    ELEVATED = "elevated"
    HIGH = "high"


class SolutionChangeKind(StrEnum):
    """The canonical trigger class for a bounded ImpactGraph input."""

    REQUIREMENT = "requirement"
    MODULE = "module"
    INTERFACE = "interface"
    CANDIDATE = "candidate"
    EVIDENCE = "evidence"
    OBSERVATION = "observation"
    VERIFICATION = "verification"


class SolutionChangeSet(BaseModel):
    """Typed, version-pinned input for impact analysis.

    It is an execution contract, not a mutable project fact: the caller stores
    its body in an Artifact and lets the ImpactGraph checkpoint retain only its
    reference.  A change set therefore cannot silently drift with the current
    project/module graph while a run is waiting or recovering.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "solution-change-set-v1"
    change_set_id: UUID
    project_id: UUID
    base_solution_version_id: UUID
    base_solution_basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    changed_module_ids: tuple[UUID, ...] = Field(min_length=1, max_length=128)
    change_kinds: tuple[SolutionChangeKind, ...] = Field(min_length=1, max_length=16)
    before_refs: tuple[str, ...] = Field(default=(), max_length=256)
    after_refs: tuple[str, ...] = Field(default=(), max_length=256)
    trigger_ref: str = Field(min_length=1, max_length=500)
    scope_ref: str | None = Field(default=None, max_length=500)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def fields_are_unique_and_hash_matches(self) -> SolutionChangeSet:
        for name in ("changed_module_ids", "change_kinds", "before_refs", "after_refs"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"SolutionChangeSet {name} must be unique")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_hash = sha256(canonical.encode()).hexdigest()
        if self.content_hash is not None and self.content_hash != expected_hash:
            raise ValueError("content_hash does not match SolutionChangeSet content")
        object.__setattr__(self, "content_hash", expected_hash)
        return self


class SolutionCoverageKey(BaseModel):
    """One solution obligation derived from the current approved module basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    module_ref: str = Field(min_length=1, max_length=500)
    requirement_ref: str = Field(min_length=1, max_length=500)
    question: str = Field(min_length=1, max_length=2_000)


class SolutionCoverageContract(BaseModel):
    """Content-addressed set of solution obligations for a frozen run basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "solution-coverage-contract-v1"
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    keys: tuple[SolutionCoverageKey, ...] = Field(min_length=1, max_length=128)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def keys_are_unique_and_hash_matches(self) -> SolutionCoverageContract:
        if len({item.key for item in self.keys}) != len(self.keys):
            raise ValueError("SolutionCoverageContract keys must be unique")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_hash = sha256(canonical.encode()).hexdigest()
        if self.content_hash is not None and self.content_hash != expected_hash:
            raise ValueError("content_hash does not match SolutionCoverageContract content")
        object.__setattr__(self, "content_hash", expected_hash)
        return self


def compile_solution_coverage_contract(
    *,
    basis_hash: str,
    requirement_ref: str,
    modules: tuple[tuple[str, str, str], ...],
) -> SolutionCoverageContract:
    """Compile one must-solve obligation per active module without model discretion.

    ``modules`` carries ``(module_key, module_ref, responsibility)`` tuples.
    It is intentionally ref-only so this compiler cannot become a second
    Domain owner.
    """

    keys = tuple(
        SolutionCoverageKey(
            key=f"solution.{module_key}.composition",
            module_ref=module_ref,
            requirement_ref=requirement_ref,
            question=f"Compose a bounded solution element for {responsibility}",
        )
        for module_key, module_ref, responsibility in sorted(modules)
    )
    return SolutionCoverageContract(basis_hash=basis_hash, keys=keys)


class SolutionContract(BaseModel):
    """Immutable admitted input boundary for one bounded Solution AgentRun.

    Each field is a stable reference or a short policy identity.  Canonical
    Requirement, Decision, Evidence, and Module bodies remain owned by Domain
    or Artifact storage and must be read through the corresponding adapter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "solution-contract-v1"
    solution_run_id: UUID
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis_project_revision: int = Field(ge=1)
    requirement_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    module_revision_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    accepted_decision_refs: tuple[str, ...] = Field(max_length=128)
    admitted_evidence_refs: tuple[str, ...] = Field(max_length=256)
    constraint_set_ref: str = Field(min_length=1, max_length=500)
    interface_contract_refs: tuple[str, ...] = Field(max_length=128)
    coverage_contract_ref: str = Field(min_length=1, max_length=500)
    risk_class: SolutionRiskClass
    allowed_tool_ids: tuple[str, ...] = Field(max_length=32)
    budget_ref: str = Field(min_length=1, max_length=500)
    verification_policy_ref: str = Field(min_length=1, max_length=500)
    completion_policy_ref: str = Field(min_length=1, max_length=500)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def references_are_unique_and_hash_matches(self) -> SolutionContract:
        reference_fields = (
            "requirement_refs",
            "module_revision_refs",
            "accepted_decision_refs",
            "admitted_evidence_refs",
            "interface_contract_refs",
            "allowed_tool_ids",
        )
        for field_name in reference_fields:
            values = getattr(self, field_name)
            if len(set(values)) != len(values):
                raise ValueError(f"SolutionContract {field_name} must be unique")

        payload = self.model_dump(mode="json", exclude={"content_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_hash = sha256(canonical.encode()).hexdigest()
        if self.content_hash is not None and self.content_hash != expected_hash:
            raise ValueError("content_hash does not match SolutionContract content")
        object.__setattr__(self, "content_hash", expected_hash)
        return self


class SolutionGraphIdentity(BaseModel):
    """Single-writer root identity projection for one Solution AgentRun."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis_project_revision: int = Field(ge=1)
    graph_revision: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    state_schema_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    solution_contract_ref: str = Field(min_length=1, max_length=500)
    solution_contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class SolutionGraphState(TypedDict, total=False):
    """Compact SolutionGraph projection; semantic conclusions stay in artifacts.

    Task execution uses the shared ``TaskEnvelope`` / ``ExecutionGrant``
    contract.  Only admitted result references can reach this root state.
    """

    identity: SolutionGraphIdentity
    task_envelope_refs: tuple[str, ...]
    admitted_result_refs: Annotated[tuple[AdmittedResultRef, ...], merge_admitted_result_refs]
    admission_record_refs: tuple[str, ...]
    solution_element_projection_ref: str
    interface_matrix_ref: str
    integration_check_ref: str
    coverage_projection_ref: str
    conflict_projection_ref: str
    sufficiency_outcome_ref: str
    proposal_manifest_ref: str


__all__ = [
    "SolutionContract",
    "SolutionChangeKind",
    "SolutionChangeSet",
    "SolutionCoverageContract",
    "SolutionCoverageKey",
    "SolutionGraphIdentity",
    "SolutionGraphState",
    "SolutionRiskClass",
    "compile_solution_coverage_contract",
]
