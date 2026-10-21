from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.domain.models import CompatibilityStatus, EvidenceStatus


class AgentOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InitialModuleDraft(AgentOutput):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=160)
    responsibility: str = Field(min_length=1, max_length=4_000)
    dependency_keys: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


class InitialModuleDiscoveryPayload(AgentOutput):
    """Untrusted initial module boundary proposal; the user still applies it."""

    summary: str = Field(min_length=1, max_length=4_000)
    modules: tuple[InitialModuleDraft, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def module_keys_are_unique(self) -> InitialModuleDiscoveryPayload:
        keys = {item.key for item in self.modules}
        if len(keys) != len(self.modules):
            raise ValueError("initial module keys must be unique")
        known = {item.key for item in self.modules}
        for item in self.modules:
            if any(key not in known or key == item.key for key in item.dependency_keys):
                raise ValueError(
                    "initial module dependencies must reference another proposed module"
                )
        dependencies_by_key = {
            item.key: item.dependency_keys for item in self.modules
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("initial module dependencies must be acyclic")
            if key in visited:
                return
            visiting.add(key)
            for dependency in dependencies_by_key[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in dependencies_by_key:
            visit(key)
        return self


class EvidenceDraft(AgentOutput):
    module_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    claim: str = Field(min_length=1, max_length=8_000)
    source_url: str = Field(min_length=1, max_length=4_000)
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    span_text: str = Field(min_length=1, max_length=16_000)
    status: EvidenceStatus
    applicability: tuple[str, ...] = ()
    provenance_snapshot_hashes: tuple[str, ...] = Field(default=(), max_length=24)

    @model_validator(mode="after")
    def preserves_primary_snapshot_provenance(self) -> EvidenceDraft:
        provenance = tuple(dict.fromkeys((self.snapshot_hash, *self.provenance_snapshot_hashes)))
        if len(provenance) > 24:
            raise ValueError("evidence provenance exceeds the bounded limit")
        if any(not _is_sha256(item) for item in provenance):
            raise ValueError("evidence provenance must contain SHA-256 hashes")
        object.__setattr__(self, "provenance_snapshot_hashes", provenance)
        return self


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


class CandidateDraft(AgentOutput):
    module_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=8_000)
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence_indexes: tuple[int, ...] = ()
    risks: tuple[str, ...] = ()


class CompatibilityDraft(AgentOutput):
    module_keys: tuple[str, ...] = Field(min_length=1, max_length=8)
    rule_id: str = Field(min_length=1, max_length=200)
    status: CompatibilityStatus
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_indexes: tuple[int, ...] = ()
    required_test: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def needs_test_has_instructions(self) -> CompatibilityDraft:
        if self.status is CompatibilityStatus.NEEDS_TEST and not self.required_test:
            raise ValueError("needs_test compatibility requires required_test")
        return self


class DecisionOptionDraft(AgentOutput):
    option_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,47}$")
    label: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4_000)
    candidate_indexes: tuple[int, ...] = Field(min_length=1, max_length=8)
    evidence_indexes: tuple[int, ...] = Field(min_length=1, max_length=24)
    risks: tuple[str, ...] = ()


class ResearchProposalPayload(AgentOutput):
    """Untrusted typed Agent output; identity and basis are injected by the mapper."""

    evidence: tuple[EvidenceDraft, ...] = Field(min_length=1, max_length=24)
    candidates: tuple[CandidateDraft, ...] = Field(min_length=1, max_length=16)
    findings: tuple[CompatibilityDraft, ...] = Field(max_length=16)
    decision_question: str = Field(min_length=1, max_length=4_000)
    decision_options: tuple[DecisionOptionDraft, ...] = Field(min_length=2, max_length=8)
    bounded_gaps: tuple[str, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def references_existing_evidence(self) -> ResearchProposalPayload:
        evidence_upper = len(self.evidence)
        referenced_evidence = (
            *(index for item in self.candidates for index in item.evidence_indexes),
            *(index for item in self.findings for index in item.evidence_indexes),
            *(index for item in self.decision_options for index in item.evidence_indexes),
        )
        if any(index < 0 or index >= evidence_upper for index in referenced_evidence):
            raise ValueError("evidence index is outside the proposal")
        candidate_upper = len(self.candidates)
        referenced_candidates = (
            index for item in self.decision_options for index in item.candidate_indexes
        )
        if any(index < 0 or index >= candidate_upper for index in referenced_candidates):
            raise ValueError("candidate index is outside the proposal")
        option_ids = tuple(item.option_id for item in self.decision_options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("decision option IDs must be unique")
        return self


class ResearchProposalReviewPayload(AgentOutput):
    """Independent, read-only review of a generated research proposal."""

    verdict: Literal["approved", "rejected"]
    reasons: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def rejected_review_requires_a_reason(self) -> ResearchProposalReviewPayload:
        if self.verdict == "rejected" and not self.reasons:
            raise ValueError("rejected review requires at least one reason")
        return self


class ModuleSelectionDraft(AgentOutput):
    module_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    candidate_id: UUID
    candidate_name: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=8_000)
    evidence_binding_ids: tuple[UUID, ...] = ()
    risks: tuple[str, ...] = ()


class BomItemDraft(AgentOutput):
    line_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    module_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    candidate_id: UUID
    name: str = Field(min_length=1, max_length=300)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=40)
    evidence_binding_ids: tuple[UUID, ...] = ()


class SolutionPlanStepDraft(AgentOutput):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    title: str = Field(min_length=1, max_length=300)
    instruction: str = Field(min_length=1, max_length=8_000)
    module_keys: tuple[str, ...] = Field(min_length=1, max_length=8)
    acceptance: tuple[str, ...] = ()


class SolutionProposalPayload(AgentOutput):
    """Untrusted solution proposal; the application validates every canonical reference."""

    module_selections: tuple[ModuleSelectionDraft, ...] = Field(min_length=1, max_length=8)
    evidence_binding_ids: tuple[UUID, ...] = ()
    compatibility_finding_ids: tuple[UUID, ...] = ()
    bom: tuple[BomItemDraft, ...] = Field(max_length=64)
    implementation_steps: tuple[SolutionPlanStepDraft, ...] = Field(min_length=1, max_length=32)
    verification_steps: tuple[SolutionPlanStepDraft, ...] = Field(min_length=1, max_length=32)
    risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    consequences: tuple[str, ...] = ()

    @model_validator(mode="after")
    def stable_keys_are_unique(self) -> SolutionProposalPayload:
        collections = (
            (item.module_key for item in self.module_selections),
            (item.line_id for item in self.bom),
            (item.step_id for item in self.implementation_steps),
            (item.step_id for item in self.verification_steps),
        )
        for values in collections:
            materialized = tuple(values)
            if len(materialized) != len(set(materialized)):
                raise ValueError("solution proposal stable keys must be unique")
        return self


class ModulePatchDraft(AgentOutput):
    module_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    base_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_id: UUID
    candidate_name: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=8_000)
    evidence_binding_ids: tuple[UUID, ...] = ()
    risks: tuple[str, ...] = ()


class ImpactProposalPayload(AgentOutput):
    """Untrusted impact proposal; deterministic closure remains server-owned."""

    summary: str = Field(min_length=1, max_length=8_000)
    stale_evidence_binding_ids: tuple[UUID, ...] = ()
    module_patches: tuple[ModulePatchDraft, ...] = Field(min_length=1, max_length=8)
    replacement_bom_items: tuple[BomItemDraft, ...] = Field(max_length=64)
    replacement_implementation_steps: tuple[SolutionPlanStepDraft, ...] = Field(
        min_length=1,
        max_length=32,
    )
    replacement_verification_steps: tuple[SolutionPlanStepDraft, ...] = Field(
        min_length=1,
        max_length=32,
    )
    risks: tuple[str, ...] = ()

    @model_validator(mode="after")
    def stable_keys_are_unique(self) -> ImpactProposalPayload:
        collections = (
            (item.module_key for item in self.module_patches),
            (item.line_id for item in self.replacement_bom_items),
            (item.step_id for item in self.replacement_implementation_steps),
            (item.step_id for item in self.replacement_verification_steps),
        )
        for values in collections:
            materialized = tuple(values)
            if len(materialized) != len(set(materialized)):
                raise ValueError("impact proposal stable keys must be unique")
        return self
