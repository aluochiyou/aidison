from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.domain.models import (
    Candidate,
    CompatibilityFinding,
    DecisionOption,
    EvidenceBinding,
    UserAdjustmentKind,
)
from aidison.evaluation.regression import AgentRunRegressionOracle


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(ApiModel):
    name: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=8_000)


class UploadProjectSourceDocumentRequest(ApiModel):
    """Text input is deliberately bounded before it reaches durable storage."""

    name: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=96_000)
    media_type: Literal["text/plain", "text/markdown", "application/json"] = "text/markdown"


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
    # Optional clarified-context fields collected during pre-approval
    # conversation rounds. Budget here is only a requirement constraint; it
    # never auto-creates a SpendBudgetRevision or any other side effect.
    usage_context: str = Field(default="", max_length=4_000)
    budget_context: str = Field(default="", max_length=4_000)
    skill_context: str = Field(default="", max_length=4_000)
    unknowns: tuple[str, ...] = ()
    modules: tuple[ModuleInput, ...] = Field(default=(), max_length=8)


class ModuleDiscoveryRequest(ApiModel):
    """User steering for an inert initial module-boundary proposal."""

    planning_brief: str | None = Field(default=None, min_length=1, max_length=2_000)
    structure_depth: Literal["focused", "standard", "deep"] = "deep"


class ResearchProposalRequest(ApiModel):
    evidence: tuple[EvidenceBinding, ...]
    candidates: tuple[Candidate, ...]
    findings: tuple[CompatibilityFinding, ...]
    decision_question: str = Field(min_length=1, max_length=4_000)
    decision_options: tuple[DecisionOption, ...] = Field(min_length=2)


class ResolveDecisionRequest(ApiModel):
    selected_option_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_plan_id: UUID


class FreezeSolutionRequest(ApiModel):
    solution_proposal_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class SubmitObservationRequest(ApiModel):
    statement: str = Field(min_length=1, max_length=8_000)
    affected_module_ids: tuple[UUID, ...] = Field(min_length=1)
    execution_plan_id: UUID


class ApprovePatchRequest(ApiModel):
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResearchStrategyPlanRequest(ApiModel):
    """User constraints for an AI-proposed, reviewable research strategy."""

    objective: str | None = Field(default=None, min_length=1, max_length=2_000)
    module_ids: tuple[UUID, ...] = Field(default=(), max_length=8)
    # Omitted means "use the selected depth profile". An explicit false is a
    # user-approved cost/time trade-off and must remain distinguishable from
    # an older client that simply did not know this field existed.
    requires_independent_verification: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=16)
    max_token_budget: int | None = Field(default=None, gt=0, le=1_000_000_000)
    max_duration_seconds: int | None = Field(default=None, ge=60, le=604_800)
    research_depth: Literal["focused", "standard", "deep"] = "deep"
    source_strategy: Literal["primary", "independent", "official", "mixed"] | None = None


class ResolveExecutionPlanRequest(ApiModel):
    decision: Literal["approved", "rejected"]
    scope_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    # A user-facing research plan can explicitly couple its approval to the
    # creation of its one bounded AgentRun. The server still validates the
    # approved plan before any run is queued.
    enqueue_research: bool = False


class CreateSelectionLockRequest(ApiModel):
    candidate_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=2_000)


class RecordUserAdjustmentRequest(ApiModel):
    kind: UserAdjustmentKind
    target: dict[str, object] = Field(min_length=1)


class SaveSolutionSnapshotRequest(ApiModel):
    label: str = Field(min_length=1, max_length=160)


class StartResearchRunRequest(ApiModel):
    execution_plan_id: UUID
    retry_failed: bool = False


class StartSolutionRunRequest(ApiModel):
    decision_id: UUID
    risk_class: Literal["standard", "elevated", "high"] = "standard"


class StartImpactRunRequest(ApiModel):
    observation_id: UUID


class ResolveAgentRunDecisionRequest(ApiModel):
    decision: Literal["approved", "rejected"]
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReviewFailureRegressionCandidateRequest(ApiModel):
    decision: Literal["approved", "rejected"]
    reviewed_by: str = Field(min_length=1, max_length=160)
    review_notes: str = Field(min_length=1, max_length=4_000)
    expected_outcome: str | None = Field(default=None, min_length=1, max_length=4_000)
    oracle: AgentRunRegressionOracle | None = None

    @model_validator(mode="after")
    def labels_match_decision(self) -> ReviewFailureRegressionCandidateRequest:
        if self.decision == "approved":
            if self.expected_outcome is None or self.oracle is None:
                raise ValueError("approved review requires expected_outcome and oracle")
        elif self.expected_outcome is not None or self.oracle is not None:
            raise ValueError("rejected review cannot carry a Golden Task oracle")
        return self


class CreateAgentRunControlRequest(ApiModel):
    kind: Literal["pause", "runtime_steering", "basis_steering"]
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    instruction: str | None = Field(default=None, min_length=1, max_length=4_000)
    change_request_ref: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def payload_matches_control_kind(self) -> CreateAgentRunControlRequest:
        if self.kind == "pause":
            if self.instruction is not None or self.change_request_ref is not None:
                raise ValueError("pause must not carry steering payload")
        elif self.kind == "runtime_steering":
            if self.instruction is None:
                raise ValueError("runtime steering requires instruction")
            if self.change_request_ref is not None:
                raise ValueError("runtime steering cannot carry a basis-change reference")
        elif self.change_request_ref is None:
            raise ValueError("basis steering requires change_request_ref")
        elif self.instruction is not None:
            raise ValueError("basis steering cannot carry runtime instruction")
        return self


class ReshapeModuleInput(ModuleInput):
    pass


class ModuleDependencyEdgeInput(ApiModel):
    """One directed dependency edge: source must be ready before target."""

    source_module_id: UUID
    target_module_id: UUID

    @model_validator(mode="after")
    def source_and_target_must_differ(self) -> ModuleDependencyEdgeInput:
        if self.source_module_id == self.target_module_id:
            raise ValueError("module dependency edge cannot be self-referential")
        return self


class EngineeringCouplingEdgeInput(ApiModel):
    source_lineage_id: UUID
    target_lineage_id: UUID
    kind: Literal[
        "mechanical", "power", "signal", "protocol", "thermal", "mass", "cost", "evidence"
    ]
    criticality: Literal["must", "should"] = "should"
    contract_ref: str | None = Field(default=None, max_length=500)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)


class ModuleLineageChangeInput(ApiModel):
    operation: Literal["rename", "split", "merge"]
    target_new_module_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    source_module_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)


class CreateProjectReshapeRequest(ApiModel):
    target_goal: str = Field(min_length=1, max_length=8_000)
    summary: str = Field(min_length=1, max_length=4_000)
    affected_module_ids: tuple[UUID, ...] = ()
    unchanged_module_ids: tuple[UUID, ...] = ()
    new_modules: tuple[ReshapeModuleInput, ...] = ()
    lineage_changes: tuple[ModuleLineageChangeInput, ...] = ()
    dependency_edges: tuple[ModuleDependencyEdgeInput, ...] | None = Field(
        default=None, max_length=128
    )
    engineering_couplings: tuple[EngineeringCouplingEdgeInput, ...] | None = Field(
        default=None, max_length=256
    )


class ResolveProjectReshapeRequest(ApiModel):
    decision: Literal["applied", "rejected"]


class CreateRequirementsChangeRequest(ApiModel):
    target_goal: str = Field(min_length=1, max_length=8_000)
    hard_constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    available_resources: tuple[str, ...] = ()
    # Optional clarified-context fields; budget stays a plain requirement
    # constraint and never auto-creates a spend-budget revision.
    usage_context: str = Field(default="", max_length=4_000)
    budget_context: str = Field(default="", max_length=4_000)
    skill_context: str = Field(default="", max_length=4_000)
    unknowns: tuple[str, ...] = ()
    summary: str = Field(min_length=1, max_length=4_000)
    modules: tuple[ModuleInput, ...] = Field(min_length=1, max_length=8)


class ResolveRequirementsChangeRequest(ApiModel):
    decision: Literal["applied", "rejected"]


# ── V1 Shopping ─────────────────────────────────────────────────────────────


class SearchOffersRequest(ApiModel):
    query: str = Field(min_length=1, max_length=500)
    bom_line_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    region: str = Field(default="CN", min_length=2, max_length=10)
    max_results: int = Field(default=10, ge=1, le=50)


class ShoppingBudgetSelectionInput(ApiModel):
    offer_snapshot_id: UUID
    quantity: int = Field(ge=1, le=10_000)


class PreviewShoppingBudgetRequest(ApiModel):
    selections: tuple[ShoppingBudgetSelectionInput, ...] = Field(min_length=1, max_length=32)


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
    effect_approval_id: UUID


class ResolveEffectApprovalRequest(ApiModel):
    decision: Literal["approved", "denied"]
    scope_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str | None = Field(default=None, max_length=1_000)


# ── Conversation ─────────────────────────────────────────────────────────────


class PostConversationMessageRequest(ApiModel):
    content: str = Field(min_length=1, max_length=16_000)
    session_id: UUID | None = None


class AcceptConversationActionProposalRequest(ApiModel):
    pass  # All state comes from auth + project revision


class ResolveConversationClarificationRequest(ApiModel):
    response: str = Field(min_length=1, max_length=8_000)


# ── Spend Budget ──────────────────────────────────────────────────────────────


class ProposeSpendBudgetRequest(ApiModel):
    amount: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=3, max_length=3)
    summary: str = Field(min_length=1, max_length=4_000)


class ResolveSpendBudgetRequest(ApiModel):
    decision: Literal["applied", "rejected"]


class SpendBudgetCostItemInput(ApiModel):
    ref: str = Field(min_length=1, max_length=200)
    amount: str = Field(max_length=100)
    currency: str = Field(min_length=1, max_length=3)
    observed_at: str | None = None
