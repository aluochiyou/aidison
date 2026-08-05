from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aidison.domain.models import Candidate, CompatibilityFinding, DecisionOption, EvidenceBinding


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(ApiModel):
    name: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=8_000)


class ModuleInput(ApiModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=160)
    responsibility: str = Field(min_length=1, max_length=4_000)
    dependency_keys: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


class ApproveRequirementsRequest(ApiModel):
    goal: str = Field(min_length=1, max_length=8_000)
    hard_constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    available_resources: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    modules: tuple[ModuleInput, ...] = Field(min_length=1, max_length=8)


class ResearchProposalRequest(ApiModel):
    evidence: tuple[EvidenceBinding, ...]
    candidates: tuple[Candidate, ...]
    findings: tuple[CompatibilityFinding, ...]
    decision_question: str = Field(min_length=1, max_length=4_000)
    decision_options: tuple[DecisionOption, ...] = Field(min_length=2)


class ResolveDecisionRequest(ApiModel):
    selected_option_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class FreezeSolutionRequest(ApiModel):
    solution_proposal_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class SubmitObservationRequest(ApiModel):
    statement: str = Field(min_length=1, max_length=8_000)
    affected_module_ids: tuple[UUID, ...] = Field(min_length=1)


class ApprovePatchRequest(ApiModel):
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


# ── V1 Shopping ─────────────────────────────────────────────────────────────


class SearchOffersRequest(ApiModel):
    query: str = Field(min_length=1, max_length=500)
    bom_line_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    region: str = Field(default="CN", min_length=2, max_length=10)
    max_results: int = Field(default=10, ge=1, le=50)


class CreatePurchaseProposalRequest(ApiModel):
    solution_version_id: UUID
    offer_snapshot_id: UUID
    quantity: int = Field(ge=1, le=10_000)
    region: str = Field(min_length=1, max_length=10)
    currency: str = Field(min_length=1, max_length=3)
    shipping_estimate: str | None = Field(default=None, max_length=100)
    tax_estimate: str | None = Field(default=None, max_length=100)
    max_total: str = Field(min_length=1, max_length=100)


class ConfirmLinesRequest(ApiModel):
    confirmed_line_ids: tuple[str, ...] = Field(min_length=1)


class CreateCheckoutHandoffRequest(ApiModel):
    pass  # All data comes from the proposal; request body is a protocol marker.
