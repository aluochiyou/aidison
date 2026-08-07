from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from aidison.application.ports import DuplicateCommandError, OptimisticConcurrencyError
from aidison.domain.models import (
    Candidate,
    CheckoutHandoff,
    CompatibilityFinding,
    DecisionRequest,
    EffectApproval,
    EffectApprovalStatus,
    EvidenceBinding,
    ImpactAnalysis,
    Module,
    Observation,
    OfferSnapshot,
    PatchSet,
    Project,
    PurchaseProposal,
    RequirementRevision,
    SolutionProposal,
    SolutionVersion,
)


class InMemoryDomainStore:
    def __init__(self) -> None:
        self.projects: dict[UUID, Project] = {}
        self.requirements: dict[UUID, RequirementRevision] = {}
        self.modules: dict[UUID, Module] = {}
        self.evidence: dict[UUID, EvidenceBinding] = {}
        self.candidates: dict[UUID, Candidate] = {}
        self.findings: dict[UUID, CompatibilityFinding] = {}
        self.decisions: dict[UUID, DecisionRequest] = {}
        self.solution_proposals: dict[UUID, SolutionProposal] = {}
        self.solutions: dict[UUID, SolutionVersion] = {}
        self.observations: dict[UUID, Observation] = {}
        self.impacts: dict[UUID, ImpactAnalysis] = {}
        self.patch_sets: dict[UUID, PatchSet] = {}
        self.receipts: dict[str, tuple[str, str]] = {}
        self.events: list[tuple[UUID, int, str, dict[str, object]]] = []
        self.offer_snapshots: dict[UUID, OfferSnapshot] = {}
        self.purchase_proposals: dict[UUID, PurchaseProposal] = {}
        self.effect_approvals: dict[UUID, EffectApproval] = {}
        self.checkout_handoffs: dict[UUID, CheckoutHandoff] = {}

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

    async def get_project(self, project_id: UUID) -> Project | None:
        return self.projects.get(project_id)

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
        self.projects[project.id] = project

    async def add_requirement_revision(self, requirement: RequirementRevision) -> None:
        self.requirements[requirement.id] = requirement

    async def get_requirement_revision(self, requirement_id: UUID) -> RequirementRevision | None:
        return self.requirements.get(requirement_id)

    async def list_requirement_revisions(self, project_id: UUID) -> Sequence[RequirementRevision]:
        return [item for item in self.requirements.values() if item.project_id == project_id]

    async def add_modules(self, modules: Sequence[Module]) -> None:
        self.modules.update({item.id: item for item in modules})

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
        self, project_id: UUID, event_type: str, payload: dict[str, object]
    ) -> int:
        sequence = sum(1 for event in self.events if event[0] == project_id) + 1
        self.events.append((project_id, sequence, event_type, payload))
        return sequence

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None
