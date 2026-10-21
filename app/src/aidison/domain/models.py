from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aidison.engineering.coupling import EngineeringCouplingEdge
from aidison.runtime.contracts import CoordinationMode


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


class ProjectRevisionChangeKind(StrEnum):
    """Why a canonical Project revision was appended.

    The first expand migration deliberately uses ``BASELINE`` instead of
    inventing historical command detail. Later application commands may use a
    more specific kind without changing the manifest's identity contract.
    """

    BASELINE = "baseline"
    PROJECT_UPDATE = "project_update"


# ── Project-scoped Conversation ──────────────────────────────────────────────


class ConversationSessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class ConversationTurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ConversationTurnType(StrEnum):
    MESSAGE = "message"
    CLARIFICATION = "clarification"
    PROPOSAL = "proposal"


class ConversationClarificationKind(StrEnum):
    USAGE = "usage"
    REQUIREMENT = "requirement"
    CONSTRAINT = "constraint"
    MODULE = "module"
    SELECTION = "selection"
    BUDGET = "budget"
    RESOURCE = "resource"
    SKILL = "skill"
    OTHER = "other"


class ConversationClarificationCoverageItem(StrEnum):
    """The five requirement facts the pre-approval clarification rounds must cover.

    Each item maps 1:1 to a ``ConversationClarificationKind`` so a persisted,
    resolved clarification is the durable evidence that the item is covered.
    Items not yet explicit in any answer may still be covered when the project
    description already states them (a model judgement re-derived each round).
    """

    USAGE = "usage"
    BUDGET = "budget"
    RESOURCES = "resources"
    SKILL = "skill"
    CONSTRAINTS = "constraints"


class ConversationClarificationStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class ConversationActionProposalKind(StrEnum):
    REWRITE_REQUIREMENTS = "rewrite_requirements"
    ADD_MODULE = "add_module"
    START_RESEARCH = "start_research"
    CHANGE_SELECTION = "change_selection"
    RESHAPE_PROJECT = "reshape_project"
    CHANGE_SPEND_BUDGET = "change_spend_budget"
    RESTORE_SOLUTION_SNAPSHOT = "restore_solution_snapshot"


class ConversationActionProposalStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ContextSummaryStatus(StrEnum):
    """Lifecycle for a derived, project-scoped conversation summary."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    TOMBSTONED = "tombstoned"


class ConversationSession(FrozenModel):
    """A single continuous conversation; at most one active per project."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    status: ConversationSessionStatus = ConversationSessionStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None

    @model_validator(mode="after")
    def closed_session_has_timestamp(self) -> ConversationSession:
        if self.status is ConversationSessionStatus.CLOSED and self.closed_at is None:
            raise ValueError("closed conversation session requires closed_at")
        return self


class ConversationTurn(FrozenModel):
    """One immutable turn in a conversation session."""

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    sequence: int = Field(ge=1)
    role: ConversationTurnRole
    content: str = Field(min_length=1, max_length=16_000)
    model_profile: str = Field(default="human", min_length=1, max_length=200)
    turn_type: ConversationTurnType = ConversationTurnType.MESSAGE
    idempotency_key: str | None = Field(default=None, max_length=300)
    # Durable causal edge. Assistant output must name the user/proposal turn
    # it answers so retries never infer a reply from incidental sequence order.
    in_reply_to_turn_id: UUID | None = None
    # Only set on assistant turns provoked by a model call failure.
    is_fallback: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def user_turn_has_idempotency_key(self) -> ConversationTurn:
        if self.role is ConversationTurnRole.USER and self.idempotency_key is None:
            raise ValueError("user conversation turn requires idempotency_key")
        return self

    @model_validator(mode="after")
    def fallback_is_only_assistant(self) -> ConversationTurn:
        if self.is_fallback and self.role is not ConversationTurnRole.ASSISTANT:
            raise ValueError("only assistant turns may be marked as fallback")
        return self

    @model_validator(mode="after")
    def reply_edge_is_only_assistant(self) -> ConversationTurn:
        if self.in_reply_to_turn_id is not None and self.role is not ConversationTurnRole.ASSISTANT:
            raise ValueError("only assistant turns may reference an answered turn")
        return self


class ConversationClarification(FrozenModel):
    """An immutable clarification request attached to an assistant turn."""

    id: UUID = Field(default_factory=uuid4)
    turn_id: UUID
    question: str = Field(min_length=1, max_length=4_000)
    kind: ConversationClarificationKind = ConversationClarificationKind.OTHER
    status: ConversationClarificationStatus = ConversationClarificationStatus.PENDING
    resolved_by_turn_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def resolution_is_consistent(self) -> ConversationClarification:
        is_resolved = self.status is ConversationClarificationStatus.RESOLVED
        if is_resolved and self.resolved_by_turn_id is None:
            raise ValueError("resolved clarification requires resolved_by_turn_id")
        if not is_resolved and self.resolved_by_turn_id is not None:
            raise ValueError("unresolved clarification cannot have resolved_by_turn_id")
        return self


class ConversationActionProposal(FrozenModel):
    """An immutable action proposal attached to an assistant turn.
    The proposed_payload is free-form JSON until accept time;
    on accept it MUST pass the corresponding Domain command schema.
    """

    id: UUID = Field(default_factory=uuid4)
    turn_id: UUID
    kind: ConversationActionProposalKind
    summary: str = Field(min_length=1, max_length=4_000)
    proposed_payload: dict[str, Any] = Field(default_factory=dict)
    status: ConversationActionProposalStatus = ConversationActionProposalStatus.PROPOSED
    resolved_at: datetime | None = None
    resolution_turn_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def resolution_is_consistent(self) -> ConversationActionProposal:
        is_resolved = self.status in {
            ConversationActionProposalStatus.ACCEPTED,
            ConversationActionProposalStatus.REJECTED,
            ConversationActionProposalStatus.EXPIRED,
        }
        if is_resolved and self.resolved_at is None:
            raise ValueError("resolved action proposal requires resolved_at")
        if not is_resolved and self.resolved_at is not None:
            raise ValueError("proposed action proposal cannot have resolved_at")
        return self


class ContextSummary(FrozenModel):
    """Append-only derived summary for a contiguous conversation-turn range.

    This is model context only.  It is deliberately not a Project revision,
    requirement, preference, decision, or other canonical engineering fact.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    session_id: UUID
    source_start_sequence: int = Field(ge=1)
    source_end_sequence: int = Field(ge=1)
    source_event_cursor: int = Field(ge=0)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str = Field(min_length=1, max_length=16_000)
    summarizer_profile: str = Field(min_length=1, max_length=200)
    status: ContextSummaryStatus = ContextSummaryStatus.ACTIVE
    tombstone_reason: str | None = Field(default=None, max_length=2_000)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def lifecycle_and_range_are_consistent(self) -> ContextSummary:
        if self.source_end_sequence < self.source_start_sequence:
            raise ValueError("context summary end sequence must not precede start sequence")
        if self.status is ContextSummaryStatus.TOMBSTONED and not self.tombstone_reason:
            raise ValueError("tombstoned context summary requires tombstone_reason")
        if self.status is not ContextSummaryStatus.TOMBSTONED and self.tombstone_reason is not None:
            raise ValueError("only tombstoned context summaries may have tombstone_reason")
        return self


# ── Core Domain ──────────────────────────────────────────────────────────────


class RequirementStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class ExecutionPlanStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class BlueprintStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class ModuleConfigurationStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class AdjustmentBatchStatus(StrEnum):
    OPEN = "open"
    FLUSHED = "flushed"


class UserAdjustmentKind(StrEnum):
    SELECT_CANDIDATE = "select_candidate"
    SET_OPTION = "set_option"
    SET_PARAMETER = "set_parameter"
    ADD_CUSTOM_CANDIDATE = "add_custom_candidate"


class DraftHistoryKind(StrEnum):
    ADJUSTMENT = "adjustment"
    LOCK = "lock"
    UNLOCK = "unlock"
    SNAPSHOT_SAVE = "snapshot_save"
    SNAPSHOT_RESTORE = "snapshot_restore"


class ProjectReshapeStatus(StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
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
    active_blueprint_id: UUID | None = None
    active_solution_version_id: UUID | None = None
    active_spend_budget_revision_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectRevisionManifest(FrozenModel):
    """Immutable composition of one canonical Project revision.

    It references versioned domain rows rather than duplicating their bodies.
    The hash deliberately excludes storage identity and timestamp so the same
    composition can be independently verified after persistence.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    revision: int = Field(ge=1)
    parent_revision_id: UUID | None = None
    requirement_revision_id: UUID | None = None
    blueprint_id: UUID | None = None
    solution_version_id: UUID | None = None
    change_kind: ProjectRevisionChangeKind
    change_summary: str = Field(min_length=1, max_length=1_000)
    content_hash: str = Field(default="", pattern=r"^[a-f0-9]{64}$")
    approved_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def manifest_hash_is_canonical(self) -> ProjectRevisionManifest:
        payload = {
            "project_id": str(self.project_id),
            "revision": self.revision,
            "parent_revision_id": str(self.parent_revision_id)
            if self.parent_revision_id is not None
            else None,
            "requirement_revision_id": str(self.requirement_revision_id)
            if self.requirement_revision_id is not None
            else None,
            "blueprint_id": str(self.blueprint_id) if self.blueprint_id is not None else None,
            "solution_version_id": str(self.solution_version_id)
            if self.solution_version_id is not None
            else None,
            "change_kind": self.change_kind.value,
            "change_summary": self.change_summary,
        }
        expected = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.content_hash and self.content_hash != expected:
            raise ValueError("project revision manifest content_hash is not canonical")
        object.__setattr__(self, "content_hash", expected)
        return self


class ModuleLineage(FrozenModel):
    """Stable identity for a logical module across future revisions."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    stable_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=160)
    split_from_lineage_id: UUID | None = None
    merged_into_lineage_id: UUID | None = None
    retired_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def lineage_links_cannot_reference_self(self) -> ModuleLineage:
        if self.id in {self.split_from_lineage_id, self.merged_into_lineage_id}:
            raise ValueError("module lineage cannot reference itself")
        return self


class ModuleWorkstreamStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class ModuleMemoryItemKind(StrEnum):
    QUESTION = "question"
    FINDING = "finding"
    FAILED_HYPOTHESIS = "failed_hypothesis"
    METHOD = "method"
    HANDOFF = "handoff"
    PREFERENCE = "preference"


class ModuleMemoryItemStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    TOMBSTONED = "tombstoned"


class ModuleWorkstream(FrozenModel):
    """Durable cross-Run trajectory for one logical module, never an Agent process."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    module_lineage_id: UUID
    status: ModuleWorkstreamStatus = ModuleWorkstreamStatus.ACTIVE
    memory_manifest_ref: str | None = Field(default=None, max_length=500)
    last_project_revision: int | None = Field(default=None, ge=1)
    last_basis_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    optimistic_revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ModuleMemoryItem(FrozenModel):
    """A structured, evidence-referenced asset for rebuilding a new Run context."""

    id: UUID = Field(default_factory=uuid4)
    workstream_id: UUID
    kind: ModuleMemoryItemKind
    stable_key: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{1,239}$")
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: ModuleMemoryItemStatus = ModuleMemoryItemStatus.ACTIVE
    artifact_ref: str = Field(min_length=1, max_length=500)
    evidence_refs: tuple[str, ...] = Field(max_length=128)
    source_result_id: UUID | None = None
    applicability: dict[str, str] = Field(default_factory=dict, max_length=16)
    freshness_deadline: datetime | None = None
    supersedes_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def structured_refs_and_applicability_are_bounded(self) -> ModuleMemoryItem:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("module memory evidence references must be unique")
        if any(not key or not value for key, value in self.applicability.items()):
            raise ValueError("module memory applicability keys and values must be non-empty")
        if any(len(key) > 120 or len(value) > 500 for key, value in self.applicability.items()):
            raise ValueError("module memory applicability entry exceeds its bounded size")
        return self


class RequirementRevision(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    revision: int = Field(ge=1)
    status: RequirementStatus = RequirementStatus.DRAFT
    goal: str = Field(min_length=1, max_length=8_000)
    hard_constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    available_resources: tuple[str, ...] = ()
    # Clarified context captured during the pre-approval conversation rounds.
    # Optional free-text fields so old revisions without them stay valid; the
    # clarification coverage items usage/budget/skill map onto these exactly.
    usage_context: str = Field(default="", max_length=4_000)
    budget_context: str = Field(default="", max_length=4_000)
    skill_context: str = Field(default="", max_length=4_000)
    unknowns: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def approved_revision_has_timestamp(self) -> RequirementRevision:
        if self.status is RequirementStatus.APPROVED and self.approved_at is None:
            raise ValueError("approved requirement revision requires approved_at")
        return self


class ProjectBlueprint(FrozenModel):
    """An immutable, user-confirmed project/module structural skeleton."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    requirement_revision_id: UUID
    version: int = Field(ge=1)
    module_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)
    dependency_edges: tuple[tuple[UUID, UUID], ...] = Field(default=(), max_length=128)
    engineering_couplings: tuple[EngineeringCouplingEdge, ...] = Field(
        default=(), max_length=256
    )
    status: BlueprintStatus = BlueprintStatus.DRAFT
    applied_reshape_proposal_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def structure_is_consistent(self) -> ProjectBlueprint:
        if len(self.module_ids) != len(set(self.module_ids)):
            raise ValueError("blueprint module IDs must be unique")
        known = set(self.module_ids)
        if any(
            source not in known or target not in known for source, target in self.dependency_edges
        ):
            raise ValueError("blueprint dependency edges must reference known modules")
        if any(source == target for source, target in self.dependency_edges):
            raise ValueError("blueprint dependency edge cannot be self-referential")
        if len(self.dependency_edges) != len(set(self.dependency_edges)):
            raise ValueError("blueprint dependency edges must be unique")
        return self


class ModuleConfiguration(FrozenModel):
    """One immutable revision of a module's user-editable draft configuration."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    module_id: UUID
    revision: int = Field(ge=1)
    base_snapshot_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    options: dict[str, Any] = Field(default_factory=dict)
    status: ModuleConfigurationStatus = ModuleConfigurationStatus.ACTIVE
    superseded_by_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def supersession_is_consistent(self) -> ModuleConfiguration:
        if self.status is ModuleConfigurationStatus.ACTIVE and self.superseded_by_id is not None:
            raise ValueError("active module configuration cannot name a superseding revision")
        if self.status is ModuleConfigurationStatus.SUPERSEDED and self.superseded_by_id is None:
            raise ValueError("superseded module configuration requires superseded_by_id")
        if self.superseded_by_id == self.id:
            raise ValueError("module configuration cannot supersede itself")
        return self


class SelectionLock(FrozenModel):
    """An explicit user choice which later Agent work must not replace silently."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    module_id: UUID
    candidate_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=2_000)
    active: bool = True
    locked_at: datetime = Field(default_factory=utc_now)
    unlocked_at: datetime | None = None

    @model_validator(mode="after")
    def lock_lifecycle_is_consistent(self) -> SelectionLock:
        if self.active and self.unlocked_at is not None:
            raise ValueError("active selection lock cannot have unlocked_at")
        if not self.active and self.unlocked_at is None:
            raise ValueError("inactive selection lock requires unlocked_at")
        return self


class UserAdjustment(FrozenModel):
    """One append-only user edit; batching never erases this individual intent."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    module_id: UUID
    batch_id: UUID | None = None
    kind: UserAdjustmentKind
    target: dict[str, Any] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class AdjustmentBatch(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    adjustment_ids: tuple[UUID, ...] = ()
    affected_module_ids: tuple[UUID, ...] = ()
    status: AdjustmentBatchStatus = AdjustmentBatchStatus.OPEN
    window_opened_at: datetime = Field(default_factory=utc_now)
    window_closes_at: datetime | None = None
    flushed_at: datetime | None = None

    @model_validator(mode="after")
    def batch_lifecycle_is_consistent(self) -> AdjustmentBatch:
        if len(self.adjustment_ids) != len(set(self.adjustment_ids)):
            raise ValueError("adjustment batch IDs must be unique")
        if len(self.affected_module_ids) != len(set(self.affected_module_ids)):
            raise ValueError("affected module IDs must be unique")
        if self.status is AdjustmentBatchStatus.OPEN and self.flushed_at is not None:
            raise ValueError("open adjustment batch cannot have flushed_at")
        if self.status is AdjustmentBatchStatus.FLUSHED and self.flushed_at is None:
            raise ValueError("flushed adjustment batch requires flushed_at")
        return self


class DraftHistoryEntry(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    sequence: int = Field(ge=1)
    parent_entry_id: UUID | None = None
    kind: DraftHistoryKind
    adjustment_ids: tuple[UUID, ...] = ()
    draft_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=utc_now)


class SolutionSnapshot(FrozenModel):
    """A user-saved draft comparison point, never a mutable SolutionVersion."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    label: str = Field(min_length=1, max_length=160)
    blueprint_id: UUID | None = None
    module_configuration_hashes: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def configuration_hashes_are_valid(self) -> SolutionSnapshot:
        if any(
            not isinstance(value, str) or len(value) != 64
            for value in self.module_configuration_hashes.values()
        ):
            raise ValueError("solution snapshot configuration hashes must be SHA-256 values")
        return self


class ProposedModule(FrozenModel):
    """A complete new module shape inside a reshape proposal, not a live module."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=160)
    responsibility: str = Field(min_length=1, max_length=4_000)
    dependency_keys: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


class ModuleLineageOperation(StrEnum):
    RENAME = "rename"
    SPLIT = "split"
    MERGE = "merge"


class ModuleLineageChange(FrozenModel):
    """An explicit, user-reviewable lineage assertion inside a reshape."""

    operation: ModuleLineageOperation
    target_new_module_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    source_module_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def operation_has_a_valid_arity(self) -> ModuleLineageChange:
        if len(self.source_module_ids) != len(set(self.source_module_ids)):
            raise ValueError("lineage change source module IDs must be unique")
        if self.operation is ModuleLineageOperation.MERGE and len(self.source_module_ids) < 2:
            raise ValueError("merge lineage change requires at least two source modules")
        if self.operation is not ModuleLineageOperation.MERGE and len(self.source_module_ids) != 1:
            raise ValueError("rename and split lineage changes require one source module")
        return self


class ProjectReshapeProposal(FrozenModel):
    """An Agent proposal to change structure; only explicit apply may create a blueprint."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    basis_blueprint_id: UUID | None = None
    target_goal: str = Field(min_length=1, max_length=8_000)
    summary: str = Field(min_length=1, max_length=4_000)
    affected_module_ids: tuple[UUID, ...] = ()
    unchanged_module_ids: tuple[UUID, ...] = ()
    new_module_keys: tuple[str, ...] = ()
    new_modules: tuple[ProposedModule, ...] = ()
    lineage_changes: tuple[ModuleLineageChange, ...] = ()
    # ``None`` preserves module-replacement behavior. A concrete tuple,
    # including ``()``, is a complete replacement for Blueprint relationships.
    dependency_edges: tuple[tuple[UUID, UUID], ...] | None = Field(
        default=None, max_length=128
    )
    engineering_couplings: tuple[EngineeringCouplingEdge, ...] | None = Field(
        default=None, max_length=256
    )
    status: ProjectReshapeStatus = ProjectReshapeStatus.PROPOSED
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def reshape_lifecycle_and_impact_are_consistent(self) -> ProjectReshapeProposal:
        if self.status is ProjectReshapeStatus.PROPOSED and self.resolved_at is not None:
            raise ValueError("proposed reshape cannot have resolved_at")
        if self.status is not ProjectReshapeStatus.PROPOSED and self.resolved_at is None:
            raise ValueError("resolved reshape requires resolved_at")
        if set(self.affected_module_ids) & set(self.unchanged_module_ids):
            raise ValueError("reshape affected and unchanged modules must be disjoint")
        if len(self.new_module_keys) != len(set(self.new_module_keys)):
            raise ValueError("reshape new module keys must be unique")
        if len({item.key for item in self.new_modules}) != len(self.new_modules):
            raise ValueError("reshape new modules must use unique keys")
        if self.new_module_keys and self.new_module_keys != tuple(
            item.key for item in self.new_modules
        ):
            raise ValueError("reshape new_module_keys must match new_modules")
        if self.dependency_edges is not None:
            if any(source == target for source, target in self.dependency_edges):
                raise ValueError("reshape dependency edge cannot be self-referential")
            if len(self.dependency_edges) != len(set(self.dependency_edges)):
                raise ValueError("reshape dependency edges must be unique")
        target_keys = tuple(item.target_new_module_key for item in self.lineage_changes)
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("reshape lineage changes must have unique target modules")
        new_keys = {item.key for item in self.new_modules}
        if not set(target_keys).issubset(new_keys):
            raise ValueError("reshape lineage changes must target a new module")
        return self


class RequirementsChangeProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class RequirementsChangeProposal(FrozenModel):
    """A reviewable, rejectable/applicable structure and requirements rewrite.

    Accepting a conversation ``rewrite_requirements`` proposal only creates this
    proposal. No RequirementRevision, Module or Blueprint is written until the
    user explicitly applies it through the canonical resolve command.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    basis_requirement_revision_id: UUID | None = None
    basis_blueprint_id: UUID | None = None
    target_goal: str = Field(min_length=1, max_length=8_000)
    hard_constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    available_resources: tuple[str, ...] = ()
    # The three clarified context fields mirror RequirementRevision so a
    # proposed rewrite keeps usage/budget/skill context without ever creating a
    # spend-budget revision or any other side effect on its own.
    usage_context: str = Field(default="", max_length=4_000)
    budget_context: str = Field(default="", max_length=4_000)
    skill_context: str = Field(default="", max_length=4_000)
    unknowns: tuple[str, ...] = ()
    summary: str = Field(min_length=1, max_length=4_000)
    modules: tuple[ProposedModule, ...] = Field(min_length=1, max_length=8)
    status: RequirementsChangeProposalStatus = RequirementsChangeProposalStatus.PROPOSED
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def lifecycle_and_modules_are_consistent(self) -> RequirementsChangeProposal:
        is_resolved = self.status is not RequirementsChangeProposalStatus.PROPOSED
        if is_resolved and self.resolved_at is None:
            raise ValueError("resolved requirements change proposal requires resolved_at")
        if not is_resolved and self.resolved_at is not None:
            raise ValueError("proposed requirements change proposal cannot have resolved_at")
        if len({item.key for item in self.modules}) != len(self.modules):
            raise ValueError("requirements change proposal module keys must be unique")
        return self


class ResearchStrategyTask(FrozenModel):
    """One reviewable research intent proposed by the model.

    It deliberately carries no coverage keys, grants, budgets, tools or
    artifact references.  Those are compiled and frozen by the service only
    after the user approves the enclosing execution plan.
    """

    task_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=2_000)
    module_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8)
    depends_on_task_keys: tuple[str, ...] = Field(default=(), max_length=16)
    priority: Literal["must", "should"]
    expected_outputs: tuple[
        Literal["evidence", "candidate", "compatibility", "constraint"], ...
    ] = Field(min_length=1, max_length=4)
    stop_conditions: tuple[str, ...] = Field(min_length=1, max_length=4)
    # Investigation angles are not sources, Coverage Keys or direct tool
    # queries. The server compiles them into a frozen task question.
    research_lenses: tuple[str, ...] = Field(default=())

    @field_validator("research_lenses")
    @classmethod
    def research_lenses_are_distinct(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("research strategy lenses must not be blank")
        if any(len(item) > 300 for item in normalized):
            raise ValueError("research strategy lenses must be at most 300 characters")
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("research strategy lenses must be unique")
        return normalized

    @model_validator(mode="after")
    def dependency_and_module_ids_are_well_formed(self) -> ResearchStrategyTask:
        if len(set(self.module_ids)) != len(self.module_ids):
            raise ValueError("research strategy task module_ids must be unique")
        if self.task_key in self.depends_on_task_keys:
            raise ValueError("research strategy task cannot depend on itself")
        if len(set(self.depends_on_task_keys)) != len(self.depends_on_task_keys):
            raise ValueError("research strategy task dependencies must be unique")
        return self


class ResearchStrategyProposal(FrozenModel):
    """Untrusted model-proposed research shape, nested in an execution plan."""

    schema_version: str = "research-strategy-v1"
    summary: str = Field(min_length=1, max_length=4_000)
    decision_notes: tuple[str, ...] = Field(default=(), max_length=12)
    scope_module_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8)
    tasks: tuple[ResearchStrategyTask, ...] = Field(min_length=1)
    deferred_questions: tuple[str, ...] = Field(default=(), max_length=12)
    risk_notes: tuple[str, ...] = Field(default=(), max_length=12)
    source_strategy: Literal["primary", "independent", "official", "mixed"]

    @model_validator(mode="after")
    def forms_a_bounded_task_dag(self) -> ResearchStrategyProposal:
        scope = set(self.scope_module_ids)
        if len(scope) != len(self.scope_module_ids):
            raise ValueError("research strategy scope_module_ids must be unique")
        by_key = {task.task_key: task for task in self.tasks}
        if len(by_key) != len(self.tasks):
            raise ValueError("research strategy task keys must be unique")
        for task in self.tasks:
            if not set(task.module_ids).issubset(scope):
                raise ValueError("research strategy task module is outside the requested scope")
            if any(dependency not in by_key for dependency in task.depends_on_task_keys):
                raise ValueError("research strategy task dependency is unknown")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_key: str) -> None:
            if task_key in visiting:
                raise ValueError("research strategy task dependencies must be acyclic")
            if task_key in visited:
                return
            visiting.add(task_key)
            for dependency in by_key[task_key].depends_on_task_keys:
                visit(dependency)
            visiting.remove(task_key)
            visited.add(task_key)

        for task_key in by_key:
            visit(task_key)
        return self


class ExecutionPlanProposal(FrozenModel):
    """A user-approved authorization boundary for one bounded Agent run."""

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    objective: str = Field(min_length=1, max_length=2_000)
    work_summary: tuple[str, ...] = Field(min_length=1, max_length=32)
    allowed_coordination_modes: tuple[CoordinationMode, ...] = Field(min_length=1, max_length=4)
    max_concurrency: int = Field(ge=1, le=16)
    max_token_budget: int = Field(default=200_000_000, gt=0, le=1_000_000_000)
    # Measured from the first successful worker claim, not from queueing. The
    # broad default is a runaway guard, not a normal research-depth limit.
    max_duration_seconds: int = Field(default=36_000, ge=60, le=604_800)
    research_depth: Literal["focused", "standard", "deep"] = "standard"
    allowed_tool_classes: tuple[str, ...] = Field(default=(), max_length=32)
    allowed_effects: tuple[str, ...] = Field(default=(), max_length=32)
    requires_independent_verification: bool = False
    requires_result_approval: bool = True
    research_strategy: ResearchStrategyProposal | None = None
    scope_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    status: ExecutionPlanStatus = ExecutionPlanStatus.PROPOSED
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def lifecycle_and_scope_are_consistent(self) -> ExecutionPlanProposal:
        if self.status is ExecutionPlanStatus.PROPOSED and self.resolved_at is not None:
            raise ValueError("proposed execution plan cannot have resolved_at")
        if self.status is not ExecutionPlanStatus.PROPOSED and self.resolved_at is None:
            raise ValueError("resolved execution plan requires resolved_at")
        expected_hash = canonical_execution_plan_scope_hash(self)
        if self.scope_hash is not None and self.scope_hash != expected_hash:
            raise ValueError("scope_hash does not match execution plan scope")
        object.__setattr__(self, "scope_hash", expected_hash)
        return self

    @model_validator(mode="after")
    def coordination_modes_are_unique(self) -> ExecutionPlanProposal:
        if len(self.allowed_coordination_modes) != len(set(self.allowed_coordination_modes)):
            raise ValueError("allowed_coordination_modes must be unique")
        return self

    @property
    def authorizes_execution(self) -> bool:
        return self.status is ExecutionPlanStatus.APPROVED


def canonical_execution_plan_scope_hash(proposal: ExecutionPlanProposal) -> str:
    payload = proposal.model_dump(
        mode="json",
        exclude={"id", "scope_hash", "status", "created_at", "resolved_at"},
    )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


class Module(FrozenModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    requirement_revision_id: UUID
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=160)
    responsibility: str = Field(min_length=1, max_length=4_000)
    lineage_id: UUID | None = None
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
    provenance_snapshot_hashes: tuple[str, ...] = Field(default=(), max_length=24)
    observed_at: datetime

    @model_validator(mode="after")
    def preserves_primary_snapshot_provenance(self) -> EvidenceBinding:
        provenance = tuple(dict.fromkeys((self.snapshot_hash, *self.provenance_snapshot_hashes)))
        if len(provenance) > 24:
            raise ValueError("evidence provenance exceeds the bounded limit")
        if any(not _is_sha256(item) for item in provenance):
            raise ValueError("evidence provenance must contain SHA-256 hashes")
        object.__setattr__(self, "provenance_snapshot_hashes", provenance)
        return self


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


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

    @model_validator(mode="after")
    def is_a_bounded_persistable_candidate(self) -> Candidate:
        """Keep flexible component properties safe for a JSONB fact record.

        Candidate attributes deliberately remain domain-specific: an ESC and a
        flight controller cannot share one rigid table schema. They still must
        be deterministic, bounded JSON before a model-derived value is allowed
        to cross the domain-write boundary. Evidence bindings are identities,
        not weights, so a duplicate cannot make one source look like multiple
        independent supports.
        """

        if not self.name.strip() or not self.description.strip():
            raise ValueError("candidate name and description must not be blank")
        if len(self.evidence_binding_ids) != len(set(self.evidence_binding_ids)):
            raise ValueError("candidate evidence bindings must be unique")
        try:
            encoded_attributes = json.dumps(
                self.attributes,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("candidate attributes must be JSON-serializable") from error
        if len(encoded_attributes) > 32_000:
            raise ValueError("candidate attributes exceed the 32000-byte limit")
        return self


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


class SolutionDependencyKind(StrEnum):
    MECHANICAL = "mechanical"
    ELECTRICAL = "electrical"
    DATA = "data"
    SOFTWARE = "software"
    THERMAL = "thermal"
    TIMING = "timing"


class SolutionDependency(FrozenModel):
    """A typed, evidence-bound edge frozen into one SolutionVersion."""

    producer_module_id: UUID
    consumer_module_id: UUID
    kind: SolutionDependencyKind
    interface_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    evidence_refs: tuple[str, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def dependency_is_directional_and_traceable(self) -> SolutionDependency:
        if self.producer_module_id == self.consumer_module_id:
            raise ValueError("solution dependency cannot connect a module to itself")
        if not self.evidence_refs:
            raise ValueError("solution dependency requires evidence references")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("solution dependency evidence references must be unique")
        return self


class ImpactDependencyPath(FrozenModel):
    """One deterministic, evidence-traceable path through a frozen solution graph."""

    origin_module_id: UUID
    affected_module_id: UUID
    module_path: tuple[UUID, ...] = Field(min_length=1, max_length=128)
    interface_keys: tuple[str, ...] = Field(max_length=127)
    evidence_refs: tuple[str, ...] = Field(max_length=1_024)

    @model_validator(mode="after")
    def path_is_contiguous_and_identified(self) -> ImpactDependencyPath:
        if self.module_path[0] != self.origin_module_id:
            raise ValueError("impact dependency path must start at its origin module")
        if self.module_path[-1] != self.affected_module_id:
            raise ValueError("impact dependency path must end at its affected module")
        if len(self.interface_keys) != len(self.module_path) - 1:
            raise ValueError("impact dependency path interface count must match module hops")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("impact dependency path evidence references must be unique")
        return self


class ImpactDisposition(StrEnum):
    """A bounded next action for a module affected by a frozen change set."""

    UNAFFECTED = "unaffected"
    REVALIDATE = "revalidate"
    REVERIFY = "reverify"
    RESEARCH = "research"
    RECOMPUTE = "recompute"
    REDECIDE = "redecide"
    OBSOLETE = "obsolete"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ImpactClassification(FrozenModel):
    """A traceable deterministic action classification inside an impact frontier."""

    module_id: UUID
    disposition: ImpactDisposition
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    dependency_path: ImpactDependencyPath | None = None
    semantic_analysis_required: bool = False

    @model_validator(mode="after")
    def classification_path_targets_its_module(self) -> ImpactClassification:
        if (
            self.dependency_path is not None
            and self.dependency_path.affected_module_id != self.module_id
        ):
            raise ValueError("impact classification path must target its module")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("impact classification reason codes must be unique")
        return self


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
    dependencies: tuple[SolutionDependency, ...] = ()
    dependency_projection_complete: bool = False
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
        selected_modules = set(module_ids)
        dependency_keys = [
            (item.producer_module_id, item.consumer_module_id, item.kind, item.interface_key)
            for item in self.dependencies
        ]
        if len(set(dependency_keys)) != len(dependency_keys):
            raise ValueError("solution proposal dependencies must be unique")
        if any(
            item.producer_module_id not in selected_modules
            or item.consumer_module_id not in selected_modules
            for item in self.dependencies
        ):
            raise ValueError("solution proposal dependency is outside selected modules")
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
    dependencies: tuple[SolutionDependency, ...] = ()
    dependency_projection_complete: bool = False
    bom: tuple[dict[str, Any], ...] = ()
    implementation_steps: tuple[dict[str, Any], ...] = ()
    verification_steps: tuple[dict[str, Any], ...] = ()
    approved_decision_id: UUID
    solution_proposal_id: UUID | None = None
    previous_version_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def frozen_dependencies_stay_within_the_solution(self) -> SolutionVersion:
        # Historical solutions did not carry a complete dependency projection
        # and may use older snapshot shapes.  They remain readable through the
        # explicit compatibility path; all new complete projections require a
        # stable module identity even when they contain zero interface edges.
        if not self.dependency_projection_complete and not self.dependencies:
            return self
        try:
            module_ids = [UUID(str(item["module_id"])) for item in self.module_snapshots]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("dependency-projected solution snapshots require module_id") from exc
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("solution version module snapshots must be unique")
        dependency_keys = [
            (item.producer_module_id, item.consumer_module_id, item.kind, item.interface_key)
            for item in self.dependencies
        ]
        if len(dependency_keys) != len(set(dependency_keys)):
            raise ValueError("solution version dependencies must be unique")
        if any(
            item.producer_module_id not in set(module_ids)
            or item.consumer_module_id not in set(module_ids)
            for item in self.dependencies
        ):
            raise ValueError("solution version dependency is outside frozen module snapshots")
        return self


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
    dependency_paths: tuple[ImpactDependencyPath, ...] = ()
    classifications: tuple[ImpactClassification, ...] = ()
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
        if self.dependency_paths and {
            item.affected_module_id for item in self.dependency_paths
        } != set(self.affected_module_ids):
            raise ValueError("impact dependency paths must cover every affected module once")
        if len({item.affected_module_id for item in self.dependency_paths}) != len(
            self.dependency_paths
        ):
            raise ValueError("impact dependency paths must have unique affected modules")
        if self.classifications and {item.module_id for item in self.classifications} != set(
            self.affected_module_ids
        ):
            raise ValueError("impact classifications must cover every affected module once")
        if len({item.module_id for item in self.classifications}) != len(self.classifications):
            raise ValueError("impact classifications must have unique modules")
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


class ChangeImpactPreviewStatus(StrEnum):
    PROPOSED = "proposed"


class ImpactedPendingRef(FrozenModel):
    """A pending research/recommendation artifact invalidated by a draft change."""

    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    entity_id: UUID
    summary: str = Field(min_length=1, max_length=4_000)
    reason: str = Field(min_length=1, max_length=4_000)


class ChangeImpactPreview(FrozenModel):
    """Read-only, traceable impact analysis of a user adjustment batch.

    Never rewrites facts. It records which modules are directly and transitively
    affected through the module dependency graph, which active selection locks
    are implicated, and which pending research/recommendation artifacts are
    invalidated. Follow-up deep re-research remains gated by an approved
    ExecutionPlanProposal; this preview never starts model or tool work.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    batch_id: UUID
    adjustment_ids: tuple[UUID, ...] = Field(min_length=1)
    basis_project_revision: int = Field(ge=1)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    direct_affected_module_ids: tuple[UUID, ...] = ()
    transitive_affected_module_ids: tuple[UUID, ...] = ()
    affected_module_ids: tuple[UUID, ...] = Field(min_length=1)
    unaffected_module_ids: tuple[UUID, ...] = ()
    affected_selection_lock_ids: tuple[UUID, ...] = ()
    invalidated_refs: tuple[ImpactedPendingRef, ...] = ()
    summary: str = Field(min_length=1, max_length=4_000)
    status: ChangeImpactPreviewStatus = ChangeImpactPreviewStatus.PROPOSED
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def impact_partition_is_consistent(self) -> ChangeImpactPreview:
        if set(self.direct_affected_module_ids) & set(self.transitive_affected_module_ids):
            raise ValueError("direct and transitive impact sets must be disjoint")
        if set(self.direct_affected_module_ids) | set(self.transitive_affected_module_ids) not in (
            set(),
            set(self.affected_module_ids),
        ):
            raise ValueError("direct and transitive impact sets must equal affected modules")
        if set(self.affected_module_ids) & set(self.unaffected_module_ids):
            raise ValueError("affected and unaffected modules must be disjoint")
        if len(self.adjustment_ids) != len(set(self.adjustment_ids)):
            raise ValueError("preview adjustment IDs must be unique")
        return self


# ── Spend Budget ─────────────────────────────────────────────────────────────


class SpendBudgetProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class SpendBudgetImpactClassification(StrEnum):
    WITHIN = "within"
    OVER = "over"
    UNKNOWN = "unknown"


class SpendBudgetCostItem(FrozenModel):
    """One known cost line submitted for budget-impact evaluation.

    ``amount`` is a decimal string; an empty string means the price is missing.
    When ``observed_at`` is ``None`` the price is considered stale or
    unobserved.
    """

    ref: str = Field(min_length=1, max_length=200)
    amount: str = Field(max_length=100)
    currency: str = Field(min_length=1, max_length=3)
    observed_at: datetime | None = None


class SpendBudgetImpactLine(FrozenModel):
    """One deterministic classification produced by a budget-impact preview."""

    line_ref: str = Field(min_length=1, max_length=200)
    amount: str = Field(max_length=100)
    currency: str = Field(min_length=1, max_length=3)
    classification: SpendBudgetImpactClassification
    reason: str = Field(min_length=1, max_length=2_000)


class ShoppingBudgetSelection(FrozenModel):
    """One user-selected immutable offer snapshot for a read-only budget preview."""

    offer_snapshot_id: UUID
    quantity: int = Field(ge=1, le=10_000)


class ShoppingBudgetSummaryLine(FrozenModel):
    """One selected offer's known and unknown costs under the Project budget."""

    offer_snapshot_id: UUID
    bom_line_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=1_000)
    quantity: int = Field(ge=1, le=10_000)
    unit_price: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=1, max_length=3)
    merchandise_subtotal: str | None = Field(default=None, max_length=100)
    total_amount: str | None = Field(default=None, max_length=100)
    classification: SpendBudgetImpactClassification
    reason: str = Field(min_length=1, max_length=2_000)


class ShoppingBudgetSummary(FrozenModel):
    """A read-only total across user-selected offers, never a purchase command."""

    project_id: UUID
    solution_version_id: UUID
    spend_budget_revision_id: UUID
    budget_amount: str = Field(min_length=1, max_length=100)
    budget_currency: str = Field(min_length=3, max_length=3)
    merchandise_subtotal: str | None = Field(default=None, max_length=100)
    total_amount: str | None = Field(default=None, max_length=100)
    remaining_amount: str | None = Field(default=None, max_length=100)
    classification: SpendBudgetImpactClassification
    lines: tuple[ShoppingBudgetSummaryLine, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def total_fields_match_classification(self) -> ShoppingBudgetSummary:
        if self.classification is SpendBudgetImpactClassification.UNKNOWN:
            if self.total_amount is not None or self.remaining_amount is not None:
                raise ValueError("unknown shopping budget summary cannot claim a final total")
        elif self.total_amount is None or self.remaining_amount is None:
            raise ValueError("known shopping budget summary requires total and remaining amounts")
        return self


class ShoppingOfferRecommendation(FrozenModel):
    """An explainable, non-binding ranking of one immutable offer snapshot."""

    rank: int = Field(ge=1)
    offer_snapshot_id: UUID
    title: str = Field(min_length=1, max_length=1_000)
    seller: str | None = Field(default=None, max_length=300)
    unit_price: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=1, max_length=3)
    product_url: str = Field(min_length=1, max_length=4_000)
    observed_at: datetime
    expires_at: datetime | None = None
    listed_price_classification: SpendBudgetImpactClassification
    final_total_known: bool
    rationale: str = Field(min_length=1, max_length=2_000)


class ShoppingOfferRecommendations(FrozenModel):
    """Read-only ranking for one active-solution BOM line."""

    project_id: UUID
    solution_version_id: UUID
    bom_line_id: str = Field(min_length=1, max_length=64)
    spend_budget_revision_id: UUID | None = None
    budget_amount: str | None = Field(default=None, max_length=100)
    budget_currency: str | None = Field(default=None, min_length=3, max_length=3)
    items: tuple[ShoppingOfferRecommendation, ...] = Field(max_length=20)


class SpendBudgetProposal(FrozenModel):
    """A user or agent proposal to set or change a project spending budget.

    Creating a proposal never modifies the active budget, Project facts,
    Job state, or product state. Only explicit resolve may create a
    ``SpendBudgetRevision``.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    amount: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=3, max_length=3)
    summary: str = Field(min_length=1, max_length=4_000)
    basis_project_revision: int = Field(ge=1)
    status: SpendBudgetProposalStatus = SpendBudgetProposalStatus.PROPOSED
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> SpendBudgetProposal:
        is_resolved = self.status is not SpendBudgetProposalStatus.PROPOSED
        if is_resolved and self.resolved_at is None:
            raise ValueError("resolved spend budget proposal requires resolved_at")
        if not is_resolved and self.resolved_at is not None:
            raise ValueError("proposed spend budget proposal cannot have resolved_at")
        return self


class SpendBudgetRevision(FrozenModel):
    """An immutable, append-only revision of the project's active spending cap.

    Only one revision per project is ACTIVE at a time. A new revision
    supersedes the prior one via CAS on the proposal status.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    proposal_id: UUID
    revision: int = Field(ge=0)
    amount: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=3, max_length=3)
    status: SpendBudgetProposalStatus = SpendBudgetProposalStatus.APPLIED
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def applied_revision_has_positive_revision(self) -> SpendBudgetRevision:
        if self.status is SpendBudgetProposalStatus.APPLIED and self.revision < 1:
            raise ValueError("applied spend budget revision must have revision >= 1")
        return self


class SpendBudgetImpactPreview(FrozenModel):
    """Read-only budget-impact analysis over a set of known cost items.

    The preview never rewrites SelectionLock, candidates, research results,
    freeze plans, or external product state. Each cost item receives exactly
    one deterministic classification.
    """

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    proposal_id: UUID
    basis_project_revision: int = Field(ge=1)
    budget_amount: str = Field(min_length=1, max_length=100)
    budget_currency: str = Field(min_length=3, max_length=3)
    lines: tuple[SpendBudgetImpactLine, ...] = ()
    summary: str = Field(min_length=1, max_length=4_000)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def lines_are_unique(self) -> SpendBudgetImpactPreview:
        if len({item.line_ref for item in self.lines}) != len(self.lines):
            raise ValueError("spend budget impact preview line refs must be unique")
        return self


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
