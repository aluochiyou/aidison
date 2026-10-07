"""Deterministic material admission for an untrusted solution-composer draft."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.solution.contracts import SolutionContract
from aidison.solution.payloads import SolutionCompositionPayload


class SolutionDraftAdmissionError(ValueError):
    """Raised when untrusted composition output escapes its frozen decision boundary."""


class ApprovedModuleCandidate(BaseModel):
    """The single candidate a Decision authorizes for one active module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module_id: UUID
    candidate_id: UUID


class SolutionDraftAdmissionInput(BaseModel):
    """All trusted material needed to check a draft without reading model prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: SolutionContract
    approved_candidates: tuple[ApprovedModuleCandidate, ...] = Field(min_length=1, max_length=64)
    approved_evidence_ids: tuple[UUID, ...] = Field(max_length=256)
    payload: SolutionCompositionPayload

    @model_validator(mode="after")
    def approved_material_is_unambiguous(self) -> SolutionDraftAdmissionInput:
        modules = [item.module_id for item in self.approved_candidates]
        if len(modules) != len(set(modules)):
            raise ValueError("approved candidates must contain one candidate per module")
        if len(self.approved_evidence_ids) != len(set(self.approved_evidence_ids)):
            raise ValueError("approved evidence IDs must be unique")
        return self


def validate_solution_draft_material(
    value: SolutionDraftAdmissionInput,
) -> SolutionCompositionPayload:
    """Accept only drafts that preserve the frozen active-module Decision selection.

    This validation is intentionally not a semantic compatibility verdict.  It
    checks only authoritative identities, material scope, and complete module
    coverage; deterministic integration and independent verification remain
    later gates.
    """

    approved_by_module = {item.module_id: item.candidate_id for item in value.approved_candidates}
    draft_by_module = {item.module_id: item for item in value.payload.elements}
    if set(draft_by_module) != set(approved_by_module):
        raise SolutionDraftAdmissionError(
            "solution draft must contain exactly one element for every approved module"
        )
    allowed_evidence = set(value.approved_evidence_ids)
    for module_id, element in draft_by_module.items():
        if element.selected_candidate_id != approved_by_module[module_id]:
            raise SolutionDraftAdmissionError(
                "solution draft selected a candidate outside the approved decision"
            )
        if not set(element.evidence_binding_ids) <= allowed_evidence:
            raise SolutionDraftAdmissionError(
                "solution draft element references evidence outside the approved decision"
            )
    for interface in value.payload.interfaces:
        if interface.producer_module_id not in approved_by_module:
            raise SolutionDraftAdmissionError(
                "solution draft interface producer is outside contract"
            )
        if interface.consumer_module_id not in approved_by_module:
            raise SolutionDraftAdmissionError(
                "solution draft interface consumer is outside contract"
            )
        if not set(interface.evidence_binding_ids) <= allowed_evidence:
            raise SolutionDraftAdmissionError(
                "solution draft interface references evidence outside the approved decision"
            )
    return value.payload


__all__ = [
    "ApprovedModuleCandidate",
    "SolutionDraftAdmissionError",
    "SolutionDraftAdmissionInput",
    "validate_solution_draft_material",
]
