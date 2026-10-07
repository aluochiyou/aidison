from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from aidison.application.ports import DuplicateCommandError, OptimisticConcurrencyError
from aidison.application.service import ProjectApplication
from aidison.domain.events import DomainEventMetadata
from aidison.domain.models import (
    AdjustmentBatch,
    AdjustmentBatchStatus,
    BlueprintStatus,
    Candidate,
    ChangeImpactPreview,
    CheckoutHandoff,
    CompatibilityFinding,
    ContextSummary,
    ConversationActionProposal,
    ConversationClarification,
    ConversationClarificationStatus,
    ConversationSession,
    ConversationTurn,
    DecisionRequest,
    DraftHistoryEntry,
    EffectApproval,
    EffectApprovalStatus,
    EvidenceBinding,
    ExecutionPlanProposal,
    ExecutionPlanStatus,
    ImpactAnalysis,
    Module,
    ModuleConfiguration,
    ModuleConfigurationStatus,
    ModuleLineage,
    ModuleMemoryItem,
    ModuleWorkstream,
    Observation,
    OfferSnapshot,
    PatchSet,
    Project,
    ProjectBlueprint,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProjectRevisionChangeKind,
    ProjectRevisionManifest,
    ProposedModule,
    PurchaseProposal,
    RequirementRevision,
    RequirementsChangeProposal,
    RequirementsChangeProposalStatus,
    SelectionLock,
    SolutionProposal,
    SolutionSnapshot,
    SolutionVersion,
    SpendBudgetImpactPreview,
    SpendBudgetProposal,
    SpendBudgetProposalStatus,
    SpendBudgetRevision,
    UserAdjustment,
)


class InMemoryDomainStore:
    def __init__(self) -> None:
        self.projects: dict[UUID, Project] = {}
        self.requirements: dict[UUID, RequirementRevision] = {}
        self.modules: dict[UUID, Module] = {}
        self.module_lineages: dict[UUID, ModuleLineage] = {}
        self.module_workstreams: dict[UUID, ModuleWorkstream] = {}
        self.module_memory_items: dict[UUID, ModuleMemoryItem] = {}
        self.project_revision_manifests: dict[UUID, ProjectRevisionManifest] = {}
        self.project_blueprints: dict[UUID, ProjectBlueprint] = {}
        self.module_configurations: dict[UUID, ModuleConfiguration] = {}
        self.selection_locks: dict[UUID, SelectionLock] = {}
        self.user_adjustments: dict[UUID, UserAdjustment] = {}
        self.adjustment_batches: dict[UUID, AdjustmentBatch] = {}
        self.draft_history_entries: dict[UUID, DraftHistoryEntry] = {}
        self.solution_snapshots: dict[UUID, SolutionSnapshot] = {}
        self.project_reshape_proposals: dict[UUID, ProjectReshapeProposal] = {}
        self.requirements_change_proposals: dict[UUID, RequirementsChangeProposal] = {}
        self.change_impact_previews: dict[UUID, ChangeImpactPreview] = {}
        self.evidence: dict[UUID, EvidenceBinding] = {}
        self.candidates: dict[UUID, Candidate] = {}
        self.findings: dict[UUID, CompatibilityFinding] = {}
        self.decisions: dict[UUID, DecisionRequest] = {}
        self.solution_proposals: dict[UUID, SolutionProposal] = {}
        self.execution_plan_proposals: dict[UUID, ExecutionPlanProposal] = {}
        self.solutions: dict[UUID, SolutionVersion] = {}
        self.observations: dict[UUID, Observation] = {}
        self.impacts: dict[UUID, ImpactAnalysis] = {}
        self.patch_sets: dict[UUID, PatchSet] = {}
        self.receipts: dict[str, tuple[str, str]] = {}
        self.events: list[tuple[UUID, int, str, dict[str, object]]] = []
        self.event_metadata: list[DomainEventMetadata | None] = []
        self.offer_snapshots: dict[UUID, OfferSnapshot] = {}
        self.purchase_proposals: dict[UUID, PurchaseProposal] = {}
        self.effect_approvals: dict[UUID, EffectApproval] = {}
        self.checkout_handoffs: dict[UUID, CheckoutHandoff] = {}
        self.conversation_sessions: dict[UUID, ConversationSession] = {}
        self.conversation_turns: dict[UUID, ConversationTurn] = {}
        self.conversation_clarifications: dict[UUID, ConversationClarification] = {}
        self.conversation_action_proposals: dict[UUID, ConversationActionProposal] = {}
        self.context_summaries: dict[UUID, ContextSummary] = {}
        self.spend_budget_proposals: dict[UUID, SpendBudgetProposal] = {}
        self.spend_budget_revisions: dict[UUID, SpendBudgetRevision] = {}
        self.spend_budget_impact_previews: dict[UUID, SpendBudgetImpactPreview] = {}

    async def claim_command(self, idempotency_key: str, payload_hash: str) -> str | None:
        receipt = self.receipts.get(idempotency_key)
        if receipt is None:
            return None
        stored_hash, result_ref = receipt
        if stored_hash != payload_hash:
            raise DuplicateCommandError("idempotency key was used with another payload")
        return result_ref

    async def save_command_receipt(
        self, idempotency_key: str, payload_hash: str, result_ref: str
    ) -> None:
        self.receipts[idempotency_key] = (payload_hash, result_ref)

    async def add_project(self, project: Project) -> None:
        self.projects[project.id] = project
        manifest = ProjectRevisionManifest(
            project_id=project.id,
            revision=project.revision,
            change_kind=ProjectRevisionChangeKind.BASELINE,
            change_summary="Project created",
            approved_at=project.created_at,
        )
        self.project_revision_manifests[manifest.id] = manifest

    async def list_project_revision_manifests(
        self, project_id: UUID
    ) -> Sequence[ProjectRevisionManifest]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.project_revision_manifests.values()
                    if item.project_id == project_id
                ),
                key=lambda item: item.revision,
            )
        )

    async def get_project(self, project_id: UUID) -> Project | None:
        return self.projects.get(project_id)

    async def get_project_event_sequence(self, project_id: UUID) -> int:
        return sum(1 for event in self.events if event[0] == project_id)

    async def list_projects(self, *, limit: int = 50) -> Sequence[Project]:
        return tuple(
            sorted(
                self.projects.values(),
                key=lambda project: project.updated_at,
                reverse=True,
            )[:limit]
        )

    async def update_project(self, project: Project, *, expected_revision: int) -> None:
        current = self.projects[project.id]
        if current.revision != expected_revision:
            raise OptimisticConcurrencyError("project revision is stale")
        previous = next(
            (
                item
                for item in self.project_revision_manifests.values()
                if item.project_id == project.id and item.revision == expected_revision
            ),
            None,
        )
        if previous is None:
            # A few narrow unit fixtures construct the aggregate dictionary
            # directly instead of calling add_project(). Production rows are
            # backfilled by the Spec-0013 migration; keep those legacy fakes
            # usable without weakening the PostgreSQL corruption boundary.
            previous = ProjectRevisionManifest(
                project_id=current.id,
                revision=expected_revision,
                requirement_revision_id=current.active_requirement_revision_id,
                blueprint_id=current.active_blueprint_id,
                solution_version_id=current.active_solution_version_id,
                change_kind=ProjectRevisionChangeKind.BASELINE,
                change_summary="In-memory fixture baseline",
                approved_at=current.updated_at,
            )
            self.project_revision_manifests[previous.id] = previous
        self.projects[project.id] = project
        manifest = ProjectRevisionManifest(
            project_id=project.id,
            revision=project.revision,
            parent_revision_id=previous.id,
            requirement_revision_id=project.active_requirement_revision_id,
            blueprint_id=project.active_blueprint_id,
            solution_version_id=project.active_solution_version_id,
            change_kind=ProjectRevisionChangeKind.PROJECT_UPDATE,
            change_summary="Canonical project state updated",
            approved_at=project.updated_at,
        )
        self.project_revision_manifests[manifest.id] = manifest

    async def add_requirement_revision(self, requirement: RequirementRevision) -> None:
        self.requirements[requirement.id] = requirement

    async def get_requirement_revision(self, requirement_id: UUID) -> RequirementRevision | None:
        return self.requirements.get(requirement_id)

    async def list_requirement_revisions(self, project_id: UUID) -> Sequence[RequirementRevision]:
        return [item for item in self.requirements.values() if item.project_id == project_id]

    async def add_modules(self, modules: Sequence[Module]) -> None:
        self.modules.update({item.id: item for item in modules})

    async def add_module_lineages(self, lineages: Sequence[ModuleLineage]) -> None:
        self.module_lineages.update({item.id: item for item in lineages})

    async def list_module_lineages(self, project_id: UUID) -> Sequence[ModuleLineage]:
        return tuple(
            sorted(
                (item for item in self.module_lineages.values() if item.project_id == project_id),
                key=lambda item: (item.stable_key, item.created_at, str(item.id)),
            )
        )

    async def update_module_lineage(self, lineage: ModuleLineage) -> None:
        existing = self.module_lineages.get(lineage.id)
        if existing is None or existing.project_id != lineage.project_id:
            raise OptimisticConcurrencyError("module lineage is missing")
        self.module_lineages[lineage.id] = lineage

    async def add_module_workstreams(self, workstreams: Sequence[ModuleWorkstream]) -> None:
        self.module_workstreams.update({item.id: item for item in workstreams})

    async def list_module_workstreams(self, project_id: UUID) -> Sequence[ModuleWorkstream]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.module_workstreams.values()
                    if item.project_id == project_id
                ),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    async def update_module_workstream(self, workstream: ModuleWorkstream) -> None:
        existing = self.module_workstreams.get(workstream.id)
        if (
            existing is None
            or existing.project_id != workstream.project_id
            or existing.optimistic_revision != workstream.optimistic_revision - 1
        ):
            raise OptimisticConcurrencyError("module workstream is stale or missing")
        self.module_workstreams[workstream.id] = workstream

    async def add_module_memory_items(self, items: Sequence[ModuleMemoryItem]) -> None:
        self.module_memory_items.update({item.id: item for item in items})

    async def list_module_memory_items(
        self,
        workstream_id: UUID,
        *,
        stable_key: str | None = None,
    ) -> Sequence[ModuleMemoryItem]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.module_memory_items.values()
                    if item.workstream_id == workstream_id
                    and (stable_key is None or item.stable_key == stable_key)
                ),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    async def assign_module_lineage(self, *, module_id: UUID, lineage_id: UUID) -> None:
        module = self.modules[module_id]
        if module.lineage_id is not None:
            raise OptimisticConcurrencyError("module lineage is already assigned or missing")
        self.modules[module_id] = module.model_copy(update={"lineage_id": lineage_id})

    async def list_modules(
        self,
        project_id: UUID,
        requirement_revision_id: UUID | None = None,
    ) -> Sequence[Module]:
        return [
            item
            for item in self.modules.values()
            if item.project_id == project_id
            and (
                requirement_revision_id is None
                or item.requirement_revision_id == requirement_revision_id
            )
        ]

    async def add_project_blueprint(self, blueprint: ProjectBlueprint) -> None:
        self.project_blueprints[blueprint.id] = blueprint

    async def get_project_blueprint(self, blueprint_id: UUID) -> ProjectBlueprint | None:
        return self.project_blueprints.get(blueprint_id)

    async def list_project_blueprints(self, project_id: UUID) -> Sequence[ProjectBlueprint]:
        return sorted(
            (item for item in self.project_blueprints.values() if item.project_id == project_id),
            key=lambda item: item.version,
        )

    async def update_project_blueprint(
        self, blueprint: ProjectBlueprint, *, expected_status: BlueprintStatus
    ) -> None:
        current = self.project_blueprints[blueprint.id]
        if current.status is not expected_status:
            raise OptimisticConcurrencyError("project blueprint status is stale")
        self.project_blueprints[blueprint.id] = blueprint

    async def add_module_configuration(self, configuration: ModuleConfiguration) -> None:
        self.module_configurations[configuration.id] = configuration

    async def list_module_configurations(
        self, project_id: UUID, module_id: UUID
    ) -> Sequence[ModuleConfiguration]:
        return sorted(
            (
                item
                for item in self.module_configurations.values()
                if item.project_id == project_id and item.module_id == module_id
            ),
            key=lambda item: item.revision,
        )

    async def update_module_configuration(
        self,
        configuration: ModuleConfiguration,
        *,
        expected_status: ModuleConfigurationStatus,
    ) -> None:
        current = self.module_configurations[configuration.id]
        if current.status is not expected_status:
            raise OptimisticConcurrencyError("module configuration status is stale")
        self.module_configurations[configuration.id] = configuration

    async def add_selection_lock(self, lock: SelectionLock) -> None:
        self.selection_locks[lock.id] = lock

    async def get_selection_lock(self, lock_id: UUID) -> SelectionLock | None:
        return self.selection_locks.get(lock_id)

    async def list_selection_locks(
        self, project_id: UUID, module_id: UUID | None = None
    ) -> Sequence[SelectionLock]:
        return [
            item
            for item in self.selection_locks.values()
            if item.project_id == project_id and (module_id is None or item.module_id == module_id)
        ]

    async def update_selection_lock(self, lock: SelectionLock, *, expected_active: bool) -> None:
        current = self.selection_locks[lock.id]
        if current.active != expected_active:
            raise OptimisticConcurrencyError("selection lock state is stale")
        self.selection_locks[lock.id] = lock

    async def add_user_adjustment(self, adjustment: UserAdjustment) -> None:
        self.user_adjustments[adjustment.id] = adjustment

    async def list_user_adjustments(
        self, project_id: UUID, batch_id: UUID | None = None
    ) -> Sequence[UserAdjustment]:
        return [
            item
            for item in self.user_adjustments.values()
            if item.project_id == project_id and (batch_id is None or item.batch_id == batch_id)
        ]

    async def add_adjustment_batch(self, batch: AdjustmentBatch) -> None:
        self.adjustment_batches[batch.id] = batch

    async def get_adjustment_batch(self, batch_id: UUID) -> AdjustmentBatch | None:
        return self.adjustment_batches.get(batch_id)

    async def list_adjustment_batches(self, project_id: UUID) -> Sequence[AdjustmentBatch]:
        return [item for item in self.adjustment_batches.values() if item.project_id == project_id]

    async def update_adjustment_batch(
        self,
        batch: AdjustmentBatch,
        *,
        expected_status: AdjustmentBatchStatus,
    ) -> None:
        current = self.adjustment_batches[batch.id]
        if current.status is not expected_status:
            raise OptimisticConcurrencyError("adjustment batch status is stale")
        self.adjustment_batches[batch.id] = batch

    async def add_draft_history_entry(self, entry: DraftHistoryEntry) -> None:
        if any(
            item.project_id == entry.project_id and item.sequence == entry.sequence
            for item in self.draft_history_entries.values()
        ):
            raise OptimisticConcurrencyError("draft history sequence already exists")
        self.draft_history_entries[entry.id] = entry

    async def list_draft_history_entries(self, project_id: UUID) -> Sequence[DraftHistoryEntry]:
        return sorted(
            (item for item in self.draft_history_entries.values() if item.project_id == project_id),
            key=lambda item: item.sequence,
        )

    async def add_solution_snapshot(self, snapshot: SolutionSnapshot) -> None:
        self.solution_snapshots[snapshot.id] = snapshot

    async def get_solution_snapshot(self, snapshot_id: UUID) -> SolutionSnapshot | None:
        return self.solution_snapshots.get(snapshot_id)

    async def list_solution_snapshots(self, project_id: UUID) -> Sequence[SolutionSnapshot]:
        return [item for item in self.solution_snapshots.values() if item.project_id == project_id]

    async def add_project_reshape_proposal(self, proposal: ProjectReshapeProposal) -> None:
        self.project_reshape_proposals[proposal.id] = proposal

    async def get_project_reshape_proposal(
        self, proposal_id: UUID
    ) -> ProjectReshapeProposal | None:
        return self.project_reshape_proposals.get(proposal_id)

    async def list_project_reshape_proposals(
        self, project_id: UUID
    ) -> Sequence[ProjectReshapeProposal]:
        return [
            item
            for item in self.project_reshape_proposals.values()
            if item.project_id == project_id
        ]

    async def update_project_reshape_proposal(
        self,
        proposal: ProjectReshapeProposal,
        *,
        expected_status: ProjectReshapeStatus,
    ) -> None:
        current = self.project_reshape_proposals[proposal.id]
        if current.status is not expected_status:
            raise OptimisticConcurrencyError("project reshape proposal status is stale")
        self.project_reshape_proposals[proposal.id] = proposal

    async def add_requirements_change_proposal(self, proposal: RequirementsChangeProposal) -> None:
        self.requirements_change_proposals[proposal.id] = proposal

    async def get_requirements_change_proposal(
        self, proposal_id: UUID
    ) -> RequirementsChangeProposal | None:
        return self.requirements_change_proposals.get(proposal_id)

    async def list_requirements_change_proposals(
        self, project_id: UUID
    ) -> Sequence[RequirementsChangeProposal]:
        return [
            item
            for item in self.requirements_change_proposals.values()
            if item.project_id == project_id
        ]

    async def update_requirements_change_proposal(
        self,
        proposal: RequirementsChangeProposal,
        *,
        expected_status: RequirementsChangeProposalStatus,
    ) -> None:
        current = self.requirements_change_proposals[proposal.id]
        if current.status is not expected_status:
            raise OptimisticConcurrencyError("requirements change proposal status is stale")
        self.requirements_change_proposals[proposal.id] = proposal

    async def add_change_impact_preview(self, preview: ChangeImpactPreview) -> None:
        self.change_impact_previews[preview.id] = preview

    async def get_change_impact_preview(self, preview_id: UUID) -> ChangeImpactPreview | None:
        return self.change_impact_previews.get(preview_id)

    async def list_change_impact_previews(self, project_id: UUID) -> Sequence[ChangeImpactPreview]:
        return [
            item for item in self.change_impact_previews.values() if item.project_id == project_id
        ]

    async def add_evidence_bindings(self, bindings: Sequence[EvidenceBinding]) -> None:
        self.evidence.update({item.id: item for item in bindings})

    async def list_evidence_bindings(self, project_id: UUID) -> Sequence[EvidenceBinding]:
        return [item for item in self.evidence.values() if item.project_id == project_id]

    async def add_candidates(self, candidates: Sequence[Candidate]) -> None:
        self.candidates.update({item.id: item for item in candidates})

    async def list_candidates(self, project_id: UUID) -> Sequence[Candidate]:
        return [item for item in self.candidates.values() if item.project_id == project_id]

    async def add_compatibility_findings(self, findings: Sequence[CompatibilityFinding]) -> None:
        self.findings.update({item.id: item for item in findings})

    async def list_compatibility_findings(self, project_id: UUID) -> Sequence[CompatibilityFinding]:
        return [item for item in self.findings.values() if item.project_id == project_id]

    async def add_decision_request(self, decision: DecisionRequest) -> None:
        self.decisions[decision.id] = decision

    async def get_decision_request(self, decision_id: UUID) -> DecisionRequest | None:
        return self.decisions.get(decision_id)

    async def list_decision_requests(self, project_id: UUID) -> Sequence[DecisionRequest]:
        return [item for item in self.decisions.values() if item.project_id == project_id]

    async def update_decision_request(self, decision: DecisionRequest) -> None:
        self.decisions[decision.id] = decision

    async def add_solution_proposal(self, proposal: SolutionProposal) -> None:
        self.solution_proposals[proposal.id] = proposal

    async def get_solution_proposal(self, proposal_id: UUID) -> SolutionProposal | None:
        return self.solution_proposals.get(proposal_id)

    async def list_solution_proposals(self, project_id: UUID) -> Sequence[SolutionProposal]:
        return [item for item in self.solution_proposals.values() if item.project_id == project_id]

    async def update_solution_proposal(self, proposal: SolutionProposal) -> None:
        self.solution_proposals[proposal.id] = proposal

    async def add_execution_plan_proposal(self, proposal: ExecutionPlanProposal) -> None:
        self.execution_plan_proposals[proposal.id] = proposal

    async def get_execution_plan_proposal(self, proposal_id: UUID) -> ExecutionPlanProposal | None:
        return self.execution_plan_proposals.get(proposal_id)

    async def list_execution_plan_proposals(
        self, project_id: UUID
    ) -> Sequence[ExecutionPlanProposal]:
        return sorted(
            (
                item
                for item in self.execution_plan_proposals.values()
                if item.project_id == project_id
            ),
            key=lambda item: (item.created_at, str(item.id)),
        )

    async def update_execution_plan_proposal(
        self,
        proposal: ExecutionPlanProposal,
        *,
        expected_status: ExecutionPlanStatus,
    ) -> None:
        current = self.execution_plan_proposals[proposal.id]
        if current.status is not expected_status:
            raise OptimisticConcurrencyError("execution plan proposal status is stale")
        self.execution_plan_proposals[proposal.id] = proposal

    async def add_solution_version(self, solution: SolutionVersion) -> None:
        self.solutions[solution.id] = solution

    async def get_solution_version(self, solution_id: UUID) -> SolutionVersion | None:
        return self.solutions.get(solution_id)

    async def list_solution_versions(self, project_id: UUID) -> Sequence[SolutionVersion]:
        return [item for item in self.solutions.values() if item.project_id == project_id]

    async def add_observation(self, observation: Observation) -> None:
        self.observations[observation.id] = observation

    async def get_observation(self, observation_id: UUID) -> Observation | None:
        return self.observations.get(observation_id)

    async def list_observations(self, project_id: UUID) -> Sequence[Observation]:
        return [item for item in self.observations.values() if item.project_id == project_id]

    async def add_impact_analysis(self, impact: ImpactAnalysis) -> None:
        self.impacts[impact.id] = impact

    async def get_impact_analysis(self, impact_id: UUID) -> ImpactAnalysis | None:
        return self.impacts.get(impact_id)

    async def list_impact_analyses(self, project_id: UUID) -> Sequence[ImpactAnalysis]:
        return [item for item in self.impacts.values() if item.project_id == project_id]

    async def update_impact_analysis(self, impact: ImpactAnalysis) -> None:
        self.impacts[impact.id] = impact

    async def add_patch_set(self, patch_set: PatchSet) -> None:
        self.patch_sets[patch_set.id] = patch_set

    async def get_patch_set(self, patch_set_id: UUID) -> PatchSet | None:
        return self.patch_sets.get(patch_set_id)

    async def list_patch_sets(self, project_id: UUID) -> Sequence[PatchSet]:
        return [item for item in self.patch_sets.values() if item.project_id == project_id]

    async def add_offer_snapshot(self, snapshot: OfferSnapshot) -> None:
        self.offer_snapshots[snapshot.id] = snapshot

    async def get_offer_snapshot(self, snapshot_id: UUID) -> OfferSnapshot | None:
        return self.offer_snapshots.get(snapshot_id)

    async def list_offer_snapshots(self, project_id: UUID) -> Sequence[OfferSnapshot]:
        return [item for item in self.offer_snapshots.values() if item.project_id == project_id]

    async def add_purchase_proposal(self, proposal: PurchaseProposal) -> None:
        self.purchase_proposals[proposal.id] = proposal

    async def get_purchase_proposal(self, proposal_id: UUID) -> PurchaseProposal | None:
        return self.purchase_proposals.get(proposal_id)

    async def list_purchase_proposals(self, project_id: UUID) -> Sequence[PurchaseProposal]:
        return [item for item in self.purchase_proposals.values() if item.project_id == project_id]

    async def update_purchase_proposal(self, proposal: PurchaseProposal) -> None:
        self.purchase_proposals[proposal.id] = proposal

    async def add_effect_approval(self, approval: EffectApproval) -> None:
        self.effect_approvals[approval.id] = approval

    async def get_effect_approval(self, approval_id: UUID) -> EffectApproval | None:
        return self.effect_approvals.get(approval_id)

    async def list_effect_approvals(self, project_id: UUID) -> Sequence[EffectApproval]:
        return [item for item in self.effect_approvals.values() if item.project_id == project_id]

    async def find_live_effect_approval(
        self,
        project_id: UUID,
        scope_hash: str,
    ) -> EffectApproval | None:
        return next(
            (
                item
                for item in self.effect_approvals.values()
                if item.project_id == project_id
                and item.scope_hash == scope_hash
                and item.status in {EffectApprovalStatus.REQUESTED, EffectApprovalStatus.APPROVED}
            ),
            None,
        )

    async def update_effect_approval(
        self,
        approval: EffectApproval,
        *,
        expected_status: EffectApprovalStatus,
    ) -> None:
        current = self.effect_approvals.get(approval.id)
        if current is None or current.status is not expected_status:
            raise OptimisticConcurrencyError("effect approval is stale or terminal")
        self.effect_approvals[approval.id] = approval

    async def add_checkout_handoff(self, handoff: CheckoutHandoff) -> None:
        self.checkout_handoffs[handoff.id] = handoff

    async def get_checkout_handoff(self, handoff_id: UUID) -> CheckoutHandoff | None:
        return self.checkout_handoffs.get(handoff_id)

    async def list_checkout_handoffs(self, project_id: UUID) -> Sequence[CheckoutHandoff]:
        return [item for item in self.checkout_handoffs.values() if item.project_id == project_id]

    async def update_checkout_handoff(self, handoff: CheckoutHandoff) -> None:
        self.checkout_handoffs[handoff.id] = handoff

    async def append_event(
        self,
        project_id: UUID,
        event_type: str,
        payload: dict[str, object],
        metadata: DomainEventMetadata | None = None,
    ) -> int:
        sequence = sum(1 for event in self.events if event[0] == project_id) + 1
        self.events.append((project_id, sequence, event_type, payload))
        self.event_metadata.append(metadata)
        return sequence

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    # ── Conversation ─────────────────────────────────────────────────────

    async def get_conversation_session(self, session_id: UUID) -> ConversationSession | None:
        return self.conversation_sessions.get(session_id)

    async def list_conversation_sessions(self, project_id: UUID) -> Sequence[ConversationSession]:
        return [
            item for item in self.conversation_sessions.values() if item.project_id == project_id
        ]

    async def get_active_conversation_session(self, project_id: UUID) -> ConversationSession | None:
        return next(
            (
                item
                for item in self.conversation_sessions.values()
                if item.project_id == project_id and item.status.value == "active"
            ),
            None,
        )

    async def add_conversation_session(self, session: ConversationSession) -> None:
        self.conversation_sessions[session.id] = session

    async def close_conversation_session(self, session_id: UUID) -> None:
        session = self.conversation_sessions[session_id]
        self.conversation_sessions[session_id] = session.model_copy(update={"status": "closed"})

    async def add_conversation_turn(self, turn: ConversationTurn) -> None:
        self.conversation_turns[turn.id] = turn

    async def lock_conversation_session(self, session_id: UUID) -> None:
        # Unit fakes run in one event loop and have no transactional backend.
        # Production serialization is covered by PostgreSQL integration tests.
        del session_id

    async def get_conversation_turn(self, turn_id: UUID) -> ConversationTurn | None:
        return self.conversation_turns.get(turn_id)

    async def get_conversation_turn_by_idempotency(
        self, session_id: UUID, idempotency_key: str
    ) -> ConversationTurn | None:
        return next(
            (
                item
                for item in self.conversation_turns.values()
                if item.session_id == session_id and item.idempotency_key == idempotency_key
            ),
            None,
        )

    async def get_conversation_reply_to_turn(self, turn_id: UUID) -> ConversationTurn | None:
        return next(
            (
                item
                for item in self.conversation_turns.values()
                if item.in_reply_to_turn_id == turn_id
            ),
            None,
        )

    async def list_conversation_turns(
        self, session_id: UUID, *, after: int = 0, limit: int = 50
    ) -> Sequence[ConversationTurn]:
        return sorted(
            (
                item
                for item in self.conversation_turns.values()
                if item.session_id == session_id and item.sequence > after
            ),
            key=lambda item: item.sequence,
        )[:limit]

    async def get_latest_turn_sequence(self, session_id: UUID) -> int:
        return max(
            (
                item.sequence
                for item in self.conversation_turns.values()
                if item.session_id == session_id
            ),
            default=0,
        )

    async def add_context_summary(self, summary: ContextSummary) -> None:
        session = self.conversation_sessions.get(summary.session_id)
        if session is None or session.project_id != summary.project_id:
            raise ValueError("context summary session must belong to its project")
        self.context_summaries[summary.id] = summary

    async def get_context_summary(self, summary_id: UUID) -> ContextSummary | None:
        return self.context_summaries.get(summary_id)

    async def find_context_summary(
        self,
        *,
        session_id: UUID,
        source_start_sequence: int,
        source_end_sequence: int,
        basis_hash: str,
    ) -> ContextSummary | None:
        return next(
            (
                item
                for item in self.context_summaries.values()
                if item.session_id == session_id
                and item.source_start_sequence == source_start_sequence
                and item.source_end_sequence == source_end_sequence
                and item.basis_hash == basis_hash
            ),
            None,
        )

    async def list_active_context_summaries(
        self, project_id: UUID, session_id: UUID
    ) -> Sequence[ContextSummary]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.context_summaries.values()
                    if item.project_id == project_id
                    and item.session_id == session_id
                    and item.status.value == "active"
                ),
                key=lambda item: (
                    item.source_end_sequence,
                    item.source_start_sequence,
                    str(item.id),
                ),
            )
        )

    async def add_conversation_clarification(
        self, clarification: ConversationClarification
    ) -> None:
        self.conversation_clarifications[clarification.id] = clarification

    async def get_conversation_clarification(
        self, clarification_id: UUID
    ) -> ConversationClarification | None:
        return self.conversation_clarifications.get(clarification_id)

    async def list_active_clarifications(
        self, session_id: UUID
    ) -> Sequence[ConversationClarification]:
        turn_session = {
            turn.id: turn.session_id for turn in self.conversation_turns.values()
        }
        return sorted(
            (
                item
                for item in self.conversation_clarifications.values()
                if item.status is ConversationClarificationStatus.PENDING
                and turn_session.get(item.turn_id) == session_id
            ),
            key=lambda item: (item.created_at, str(item.id)),
        )

    async def list_clarifications(
        self, session_id: UUID
    ) -> Sequence[ConversationClarification]:
        turn_session = {
            turn.id: turn.session_id for turn in self.conversation_turns.values()
        }
        items = [
            item
            for item in self.conversation_clarifications.values()
            if turn_session.get(item.turn_id) == session_id
        ]
        return sorted(items, key=lambda item: (item.created_at, str(item.id)))

    async def resolve_conversation_clarification(
        self, clarification_id: UUID, resolved_by_turn_id: UUID
    ) -> None:
        item = self.conversation_clarifications[clarification_id]
        self.conversation_clarifications[clarification_id] = item.model_copy(
            update={
                "status": ConversationClarificationStatus.RESOLVED,
                "resolved_by_turn_id": resolved_by_turn_id,
            }
        )

    async def add_conversation_action_proposal(self, proposal: ConversationActionProposal) -> None:
        self.conversation_action_proposals[proposal.id] = proposal

    async def get_conversation_action_proposal(
        self, proposal_id: UUID
    ) -> ConversationActionProposal | None:
        return self.conversation_action_proposals.get(proposal_id)

    async def list_active_action_proposals(
        self, session_id: UUID
    ) -> Sequence[ConversationActionProposal]:
        turn_session = {
            turn.id: turn.session_id for turn in self.conversation_turns.values()
        }
        return sorted(
            (
                item
                for item in self.conversation_action_proposals.values()
                if item.status.value == "proposed"
                and turn_session.get(item.turn_id) == session_id
            ),
            key=lambda item: (item.created_at, str(item.id)),
        )

    async def resolve_conversation_action_proposal(
        self,
        proposal_id: UUID,
        *,
        status: str,
        resolved_at: datetime,
        resolution_turn_id: UUID,
    ) -> None:
        item = self.conversation_action_proposals[proposal_id]
        self.conversation_action_proposals[proposal_id] = item.model_copy(
            update={
                "status": status,
                "resolved_at": resolved_at,
                "resolution_turn_id": resolution_turn_id,
            }
        )

    # ── Spend Budget ──────────────────────────────────────────────────────

    async def add_spend_budget_proposal(self, proposal: SpendBudgetProposal) -> None:
        self.spend_budget_proposals[proposal.id] = proposal

    async def get_spend_budget_proposal(self, proposal_id: UUID) -> SpendBudgetProposal | None:
        return self.spend_budget_proposals.get(proposal_id)

    async def list_spend_budget_proposals(self, project_id: UUID) -> Sequence[SpendBudgetProposal]:
        return [
            item for item in self.spend_budget_proposals.values() if item.project_id == project_id
        ]

    async def update_spend_budget_proposal(
        self,
        proposal: SpendBudgetProposal,
        *,
        expected_status: SpendBudgetProposalStatus,
    ) -> None:
        current = self.spend_budget_proposals[proposal.id]
        if current.status is not expected_status:
            raise OptimisticConcurrencyError("spend budget proposal status is stale")
        self.spend_budget_proposals[proposal.id] = proposal

    async def add_spend_budget_revision(self, revision: SpendBudgetRevision) -> None:
        self.spend_budget_revisions[revision.id] = revision

    async def get_spend_budget_revision(self, revision_id: UUID) -> SpendBudgetRevision | None:
        return self.spend_budget_revisions.get(revision_id)

    async def list_spend_budget_revisions(self, project_id: UUID) -> Sequence[SpendBudgetRevision]:
        return sorted(
            (
                item
                for item in self.spend_budget_revisions.values()
                if item.project_id == project_id
            ),
            key=lambda item: item.revision,
        )

    async def get_active_spend_budget(self, project_id: UUID) -> SpendBudgetRevision | None:
        project = self.projects.get(project_id)
        if project is None or project.active_spend_budget_revision_id is None:
            return None
        return self.spend_budget_revisions.get(project.active_spend_budget_revision_id)

    async def update_spend_budget_revision(
        self,
        revision: SpendBudgetRevision,
        *,
        expected_status: SpendBudgetProposalStatus,
    ) -> None:
        current = self.spend_budget_revisions.get(revision.id)
        if current is None:
            raise OptimisticConcurrencyError("spend budget revision not found")
        if current.status != expected_status:
            raise OptimisticConcurrencyError("spend budget revision status is stale")
        self.spend_budget_revisions[revision.id] = revision

    async def add_spend_budget_impact_preview(self, preview: SpendBudgetImpactPreview) -> None:
        self.spend_budget_impact_previews[preview.id] = preview

    async def get_spend_budget_impact_preview(
        self, preview_id: UUID
    ) -> SpendBudgetImpactPreview | None:
        return self.spend_budget_impact_previews.get(preview_id)

    async def list_spend_budget_impact_previews(
        self, project_id: UUID
    ) -> Sequence[SpendBudgetImpactPreview]:
        return [
            item
            for item in self.spend_budget_impact_previews.values()
            if item.project_id == project_id
        ]

def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


async def bootstrap_initial_modules(
    *,
    store: InMemoryDomainStore,
    application: ProjectApplication,
    project: Project,
    modules: Sequence[dict[str, object]],
    idempotency_key_prefix: str,
) -> tuple[Project, tuple[Module, ...]]:
    """Create test modules through the governed initial-discovery apply path.

    A unit test may supply deterministic discovery output, but it must still
    follow approved requirements -> proposed reshape -> explicit apply.  This
    prevents test fixtures from quietly restoring the removed direct-module
    requirements write path.
    """

    requirement, _ = await application.approve_requirements(
        project_id=project.id,
        expected_project_revision=project.revision,
        goal=project.goal,
        hard_constraints=(),
        preferences=(),
        available_resources=(),
        unknowns=(),
        modules=(),
        idempotency_key=f"{idempotency_key_prefix}:requirements",
    )
    after_requirements = await store.get_project(project.id)
    assert after_requirements is not None
    proposed_modules = tuple(
        ProposedModule(
            key=str(item["key"]),
            name=str(item["name"]),
            responsibility=str(item["responsibility"]),
            dependency_keys=_string_tuple(item.get("dependency_keys")),
            acceptance=_string_tuple(item.get("acceptance")),
            open_questions=_string_tuple(item.get("open_questions")),
        )
        for item in modules
    )
    proposal = await application.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            basis_blueprint_id=None,
            target_goal=requirement.goal,
            summary="Deterministic test discovery output",
            new_module_keys=tuple(item.key for item in proposed_modules),
            new_modules=proposed_modules,
        ),
        expected_project_revision=after_requirements.revision,
        idempotency_key=f"{idempotency_key_prefix}:reshape-proposal",
    )
    await application.resolve_project_reshape(
        proposal_id=proposal.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=after_requirements.revision,
        idempotency_key=f"{idempotency_key_prefix}:reshape-apply",
    )
    current = await store.get_project(project.id)
    assert current is not None
    materialized = tuple(
        await store.list_modules(project.id, requirement_revision_id=requirement.id)
    )
    return current, materialized
