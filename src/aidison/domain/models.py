from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectStage(StrEnum):
    INTAKE = "intake"
    REQUIREMENTS = "requirements"
    RESEARCH = "research"
    COMPARING = "comparing"
    DECIDING = "deciding"
    APPROVED = "approved"
    VERIFYING = "verifying"
    REVISING = "revising"


class RequirementStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class ModuleStage(StrEnum):
    DRAFT = "draft"
    RESEARCHING = "researching"
    COMPARING = "comparing"
    DECIDING = "deciding"
    SELECTED = "selected"
    VERIFYING = "verifying"
    REVISED = "revised"


class EvidenceStatus(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"
    STALE = "stale"
    RETRACTED = "retracted"


class CompatibilityStatus(StrEnum):
    COMPATIBLE = "compatible"
    CONDITIONAL = "conditional"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"
    NEEDS_TEST = "needs_test"


class DecisionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ImpactStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class SolutionProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class Project(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=8_000)
    stage: ProjectStage = ProjectStage.INTAKE
    revision: int = Field(default=1, ge=1)
    active_requirement_revision_id: UUID | None = None
    active_solution_version_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RequirementRevision(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    revision: int = Field(ge=1)
    status: RequirementStatus = RequirementStatus.DRAFT
    goal: str = Field(min_length=1, max_length=8_000)
    hard_constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    available_resources: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def approved_revision_has_timestamp(self) -> RequirementRevision:
        if self.status is RequirementStatus.APPROVED and self.approved_at is None:
            raise ValueError("approved requirement revision requires approved_at")
        return self


class Module(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    requirement_revision_id: UUID
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=160)
    responsibility: str = Field(min_length=1, max_length=4_000)
    stage: ModuleStage = ModuleStage.DRAFT
    dependency_ids: tuple[UUID, ...] = ()
    acceptance: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


class EvidenceBinding(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    module_id: UUID
    claim: str = Field(min_length=1, max_length=8_000)
    source_url: str = Field(min_length=1, max_length=4_000)
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    span_text: str = Field(min_length=1, max_length=16_000)
    status: EvidenceStatus
    applicability: tuple[str, ...] = ()
    observed_at: datetime


class CompatibilityFinding(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    module_ids: tuple[UUID, ...] = Field(min_length=1)
    rule_id: str = Field(min_length=1, max_length=200)
    status: CompatibilityStatus
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_binding_ids: tuple[UUID, ...] = ()
    required_test: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def needs_test_has_instructions(self) -> CompatibilityFinding:
        if self.status is CompatibilityStatus.NEEDS_TEST and not self.required_test:
            raise ValueError("needs_test compatibility requires required_test")
        return self


class Candidate(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    module_id: UUID
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=8_000)
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence_binding_ids: tuple[UUID, ...] = ()
    risks: tuple[str, ...] = ()


class DecisionOption(FrozenModel):
    option_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    label: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4_000)
    candidate_ids: tuple[UUID, ...] = ()
    evidence_binding_ids: tuple[UUID, ...] = ()
    risks: tuple[str, ...] = ()
    legacy_unbound: bool = False

    @model_validator(mode="after")
    def bindings_are_unique(self) -> DecisionOption:
        if not self.legacy_unbound and (not self.candidate_ids or not self.evidence_binding_ids):
            raise ValueError("new decision options must bind candidates and evidence")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("decision option candidate bindings must be unique")
        if len(self.evidence_binding_ids) != len(set(self.evidence_binding_ids)):
            raise ValueError("decision option evidence bindings must be unique")
        return self


class DecisionRequest(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    question: str = Field(min_length=1, max_length=4_000)
    options: tuple[DecisionOption, ...] = Field(min_length=2)
    affected_module_ids: tuple[UUID, ...] = ()
    status: DecisionStatus = DecisionStatus.PENDING
    selected_option_id: str | None = None
    resolved_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_string_options(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        raw_options = value.get("options")
        if not isinstance(raw_options, (list, tuple)) or not raw_options:
            return value
        if not all(isinstance(option, str) for option in raw_options):
            return value
        migrated = dict(value)
        legacy_options = [
            {
                "option_id": f"legacy-{index + 1}",
                "label": option,
                "summary": "Historical unbound decision option",
                "candidate_ids": [],
                "evidence_binding_ids": [],
                "legacy_unbound": True,
            }
            for index, option in enumerate(raw_options)
        ]
        legacy_selected = migrated.pop("selected_option", None)
        migrated["options"] = legacy_options
        if legacy_selected is not None:
            try:
                selected_index = list(raw_options).index(legacy_selected)
            except ValueError:
                selected_index = -1
            migrated["selected_option_id"] = (
                f"legacy-{selected_index + 1}" if selected_index >= 0 else None
            )
        return migrated

    @model_validator(mode="after")
    def resolution_is_consistent(self) -> DecisionRequest:
        option_ids = tuple(option.option_id for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("decision option IDs must be unique")
        if self.status in {DecisionStatus.APPROVED, DecisionStatus.REJECTED}:
            if self.selected_option_id is None or self.resolved_at is None:
                raise ValueError("resolved decision requires selected_option and resolved_at")
            if self.selected_option_id not in option_ids:
                raise ValueError("selected_option_id must identify one of the frozen options")
        return self


class ModuleSelection(FrozenModel):
    module_id: UUID
    candidate_id: UUID
    candidate_name: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=8_000)
    evidence_binding_ids: tuple[UUID, ...] = ()
    risks: tuple[str, ...] = ()


class BomItem(FrozenModel):
    line_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    module_id: UUID
    candidate_id: UUID
    name: str = Field(min_length=1, max_length=300)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=40)
    evidence_binding_ids: tuple[UUID, ...] = ()


class SolutionPlanStep(FrozenModel):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    title: str = Field(min_length=1, max_length=300)
    instruction: str = Field(min_length=1, max_length=8_000)
    module_ids: tuple[UUID, ...] = Field(min_length=1)
    acceptance: tuple[str, ...] = ()


class SolutionProposal(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    decision_id: UUID
    requirement_revision_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    module_selections: tuple[ModuleSelection, ...] = Field(min_length=1)
    evidence_binding_ids: tuple[UUID, ...] = ()
    compatibility_finding_ids: tuple[UUID, ...] = ()
    bom: tuple[BomItem, ...] = ()
    implementation_steps: tuple[SolutionPlanStep, ...] = Field(min_length=1)
    verification_steps: tuple[SolutionPlanStep, ...] = Field(min_length=1)
    risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    consequences: tuple[str, ...] = ()
    artifact_ref: str = Field(min_length=1, max_length=4_000)
    profile_id: str = Field(min_length=1, max_length=200)
    profile_revision: int = Field(ge=1)
    status: SolutionProposalStatus = SolutionProposalStatus.PROPOSED
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def proposal_is_consistent(self) -> SolutionProposal:
        module_ids = [item.module_id for item in self.module_selections]
        if len(set(module_ids)) != len(module_ids):
            raise ValueError("solution proposal must select each module once")
        if self.status is not SolutionProposalStatus.PROPOSED and self.resolved_at is None:
            raise ValueError("resolved solution proposal requires resolved_at")
        return self


class SolutionVersion(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    version: int = Field(ge=1)
    requirement_revision_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    module_snapshots: tuple[dict[str, Any], ...]
    evidence_binding_ids: tuple[UUID, ...] = ()
    compatibility_finding_ids: tuple[UUID, ...] = ()
    bom: tuple[dict[str, Any], ...] = ()
    implementation_steps: tuple[dict[str, Any], ...] = ()
    verification_steps: tuple[dict[str, Any], ...] = ()
    approved_decision_id: UUID
    solution_proposal_id: UUID | None = None
    previous_version_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Observation(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    solution_version_id: UUID
    statement: str = Field(min_length=1, max_length=8_000)
    affected_module_hints: tuple[UUID, ...] = ()
    artifact_ids: tuple[UUID, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class ModulePatch(FrozenModel):
    module_id: UUID
    base_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    replacement: ModuleSelection

    @model_validator(mode="after")
    def replacement_targets_same_module(self) -> ModulePatch:
        if self.replacement.module_id != self.module_id:
            raise ValueError("module patch replacement must target the same module")
        return self


class ImpactAnalysis(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    observation_id: UUID
    base_solution_version_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    direct_affected_module_ids: tuple[UUID, ...] = ()
    transitive_affected_module_ids: tuple[UUID, ...] = ()
    affected_module_ids: tuple[UUID, ...] = Field(min_length=1)
    unaffected_module_ids: tuple[UUID, ...] = ()
    stale_evidence_binding_ids: tuple[UUID, ...] = ()
    module_patches: tuple[ModulePatch, ...] = ()
    replacement_bom_items: tuple[BomItem, ...] = ()
    replacement_implementation_steps: tuple[SolutionPlanStep, ...] = ()
    replacement_verification_steps: tuple[SolutionPlanStep, ...] = ()
    summary: str = "Legacy impact analysis"
    risks: tuple[str, ...] = ()
    artifact_ref: str | None = None
    profile_id: str | None = None
    profile_revision: int | None = Field(default=None, ge=1)
    proposed_changes: tuple[dict[str, Any], ...] = ()
    status: ImpactStatus = ImpactStatus.PROPOSED
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def resolved_impact_has_timestamp(self) -> ImpactAnalysis:
        if self.status is not ImpactStatus.PROPOSED and self.resolved_at is None:
            raise ValueError("resolved impact analysis requires resolved_at")
        if set(self.affected_module_ids) & set(self.unaffected_module_ids):
            raise ValueError("affected and unaffected modules must be disjoint")
        if set(self.direct_affected_module_ids) & set(self.transitive_affected_module_ids):
            raise ValueError("direct and transitive impact sets must be disjoint")
        if set(self.direct_affected_module_ids) | set(self.transitive_affected_module_ids) not in (
            set(),
            set(self.affected_module_ids),
        ):
            raise ValueError("direct and transitive impact sets must equal affected modules")
        if not {item.module_id for item in self.module_patches} <= set(self.affected_module_ids):
            raise ValueError("impact patches must stay inside affected modules")
        if (self.artifact_ref is None) != (self.profile_id is None):
            raise ValueError("impact artifact and profile identity must be recorded together")
        if (self.profile_id is None) != (self.profile_revision is None):
            raise ValueError("impact profile identity requires a revision")
        return self


class PatchSet(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    impact_analysis_id: UUID
    base_solution_version_id: UUID
    base_solution_basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    module_patches: tuple[dict[str, Any], ...] = ()
    typed_module_patches: tuple[ModulePatch, ...] = ()
    replacement_bom_items: tuple[BomItem, ...] = ()
    replacement_implementation_steps: tuple[SolutionPlanStep, ...] = ()
    replacement_verification_steps: tuple[SolutionPlanStep, ...] = ()

    @model_validator(mode="after")
    def has_one_patch_contract(self) -> PatchSet:
        if not self.module_patches and not self.typed_module_patches:
            raise ValueError("patch set requires at least one module patch")
        if self.module_patches and self.typed_module_patches:
            raise ValueError("patch set cannot mix legacy and typed module patches")
        return self

    created_at: datetime = Field(default_factory=utc_now)


# ── V1 Shopping ──────────────────────────────────────────────────────────────


class PurchaseProposalStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    EXPIRED = "expired"
    HANDED_OFF = "handed_off"


class CheckoutHandoffStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    AMBIGUOUS = "ambiguous"


class HandoffKind(StrEnum):
    """What kind of external handoff the provider supports."""

    CART_REDIRECT = "cart_redirect"
    PRODUCT_REDIRECT = "product_redirect"


class EffectApprovalStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class EffectApproval(FrozenModel):
    """One expiring authorization for one server-derived external effect scope.

    The scope is bound by provider, handoff_kind, proposal, and basis.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    effect_kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,99}$")
    target_ref: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    constraints: dict[str, Any]
    status: EffectApprovalStatus = EffectApprovalStatus.REQUESTED
    requested_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    resolved_at: datetime | None = None
    consumed_at: datetime | None = None
    resolution_reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def has_consistent_lifecycle(self) -> EffectApproval:
        if self.expires_at <= self.requested_at:
            raise ValueError("effect approval expires_at must be after requested_at")
        if self.status is EffectApprovalStatus.REQUESTED:
            if self.resolved_at is not None or self.consumed_at is not None:
                raise ValueError("requested effect approval cannot be resolved or consumed")
            if self.resolution_reason is not None:
                raise ValueError("requested effect approval cannot have a resolution reason")
            return self
        if self.resolved_at is None:
            raise ValueError("resolved effect approval requires resolved_at")
        if self.status is EffectApprovalStatus.DENIED and not (
            self.resolution_reason and self.resolution_reason.strip()
        ):
            raise ValueError("denied effect approval requires a reason")
        if self.status is EffectApprovalStatus.CONSUMED:
            if self.consumed_at is None:
                raise ValueError("consumed effect approval requires consumed_at")
        elif self.consumed_at is not None:
            raise ValueError("only consumed effect approval can have consumed_at")
        return self


class OfferSnapshot(FrozenModel):
    """Immutable provider offer captured at observation time."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    solution_version_id: UUID
    bom_line_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    provider: str = Field(min_length=1, max_length=100)
    provider_offer_id: str = Field(min_length=1, max_length=500)
    merchandise_id: str | None = Field(default=None, max_length=500)
    seller: str | None = Field(default=None, max_length=300)
    title: str = Field(min_length=1, max_length=1_000)
    condition: str | None = Field(default=None, max_length=100)
    availability: str = Field(min_length=1, max_length=40)
    unit_price: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=1, max_length=3)
    shipping_estimate: str | None = Field(default=None, max_length=100)
    tax_estimate: str | None = Field(default=None, max_length=100)
    region: str = Field(min_length=1, max_length=10)
    quantity_available: int = Field(ge=0)
    product_url: str = Field(min_length=1, max_length=4_000)
    observed_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: str = Field(min_length=1, max_length=400)


class PurchaseProposal(FrozenModel):
    """A purchase proposal bound to an exact SolutionVersion and offer snapshot."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    solution_version_id: UUID
    offer_snapshot_id: UUID
    quantity: int = Field(ge=1)
    region: str = Field(min_length=1, max_length=10)
    currency: str = Field(min_length=1, max_length=3)
    shipping_estimate: str | None = Field(default=None, max_length=100)
    tax_estimate: str | None = Field(default=None, max_length=100)
    max_total: str = Field(min_length=1, max_length=100)
    unit_price: str = Field(min_length=1, max_length=100)
    status: PurchaseProposalStatus = PurchaseProposalStatus.DRAFT
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed_line_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    handed_off_at: datetime | None = None

    @model_validator(mode="after")
    def handed_off_has_timestamp(self) -> PurchaseProposal:
        if self.status is PurchaseProposalStatus.HANDED_OFF and self.handed_off_at is None:
            raise ValueError("handed_off proposal requires handed_off_at")
        return self


class CheckoutHandoff(FrozenModel):
    """A checkout handoff bridging the proposal to a provider-hosted cart or product page."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    proposal_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider: str = Field(min_length=1, max_length=100)
    handoff_kind: HandoffKind = HandoffKind.CART_REDIRECT
    provider_cart_id: str | None = Field(default=None, max_length=500)
    checkout_url: str | None = Field(default=None, max_length=4_000)
    status: CheckoutHandoffStatus = CheckoutHandoffStatus.PREPARED
    created_at: datetime = Field(default_factory=utc_now)
    dispatched_at: datetime | None = None
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def dispatched_has_cart_and_url(self) -> CheckoutHandoff:
        if self.status in {CheckoutHandoffStatus.DISPATCHED, CheckoutHandoffStatus.SUCCEEDED}:
            if not self.checkout_url:
                raise ValueError("dispatched/succeeded handoff requires checkout_url")
            if not self.checkout_url.startswith("https://"):
                raise ValueError("checkout URL must use HTTPS")
            if self.handoff_kind is HandoffKind.CART_REDIRECT and not self.provider_cart_id:
                raise ValueError(
                    "dispatched/succeeded cart_redirect handoff requires provider_cart_id"
                )
        return self

    @model_validator(mode="after")
    def resolved_has_timestamp(self) -> CheckoutHandoff:
        if self.status in {CheckoutHandoffStatus.SUCCEEDED, CheckoutHandoffStatus.AMBIGUOUS}:
            if self.resolved_at is None:
                raise ValueError("resolved handoff requires resolved_at")
        return self
