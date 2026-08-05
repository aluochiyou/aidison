from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from aidison.domain.models import (
    Candidate,
    CheckoutHandoff,
    CompatibilityFinding,
    DecisionRequest,
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


class OptimisticConcurrencyError(RuntimeError):
    """Raised when a command is based on an obsolete aggregate revision."""


class DuplicateCommandError(RuntimeError):
    """Raised when one idempotency key is reused with a different payload."""


class DomainStore(Protocol):
    """Canonical persistence boundary used by application services.

    Implementations must commit a domain mutation, its command receipt and its
    outbox events in one transaction. Read methods must not consult checkpoint,
    cache or provider conversation state.
    """

    async def claim_command(self, idempotency_key: str, payload_hash: str) -> str | None:
        """Serialize one idempotency key and return its completed result when present.

        PostgreSQL implementations must hold the claim until the surrounding
        transaction commits or rolls back. A missing result means this
        transaction owns the command execution.
        """
        ...

    async def save_command_receipt(
        self,
        idempotency_key: str,
        payload_hash: str,
        result_ref: str,
    ) -> None: ...

    async def add_project(self, project: Project) -> None: ...

    async def get_project(self, project_id: UUID) -> Project | None: ...

    async def list_projects(self, *, limit: int = 50) -> Sequence[Project]: ...

    async def update_project(self, project: Project, *, expected_revision: int) -> None: ...

    async def add_requirement_revision(self, requirement: RequirementRevision) -> None: ...

    async def get_requirement_revision(
        self,
        requirement_id: UUID,
    ) -> RequirementRevision | None: ...

    async def list_requirement_revisions(
        self,
        project_id: UUID,
    ) -> Sequence[RequirementRevision]: ...

    async def add_modules(self, modules: Sequence[Module]) -> None: ...

    async def list_modules(
        self,
        project_id: UUID,
        requirement_revision_id: UUID | None = None,
    ) -> Sequence[Module]: ...

    async def add_evidence_bindings(self, bindings: Sequence[EvidenceBinding]) -> None: ...

    async def list_evidence_bindings(
        self,
        project_id: UUID,
    ) -> Sequence[EvidenceBinding]: ...

    async def add_compatibility_findings(
        self,
        findings: Sequence[CompatibilityFinding],
    ) -> None: ...

    async def list_compatibility_findings(
        self,
        project_id: UUID,
    ) -> Sequence[CompatibilityFinding]: ...

    async def add_candidates(self, candidates: Sequence[Candidate]) -> None: ...

    async def list_candidates(self, project_id: UUID) -> Sequence[Candidate]: ...

    async def add_decision_request(self, decision: DecisionRequest) -> None: ...

    async def get_decision_request(self, decision_id: UUID) -> DecisionRequest | None: ...

    async def update_decision_request(self, decision: DecisionRequest) -> None: ...

    async def add_solution_proposal(self, proposal: SolutionProposal) -> None: ...

    async def get_solution_proposal(self, proposal_id: UUID) -> SolutionProposal | None: ...

    async def list_solution_proposals(self, project_id: UUID) -> Sequence[SolutionProposal]: ...

    async def update_solution_proposal(self, proposal: SolutionProposal) -> None: ...

    async def add_solution_version(self, solution: SolutionVersion) -> None: ...

    async def get_solution_version(self, solution_id: UUID) -> SolutionVersion | None: ...

    async def list_solution_versions(self, project_id: UUID) -> Sequence[SolutionVersion]: ...

    async def add_observation(self, observation: Observation) -> None: ...

    async def get_observation(self, observation_id: UUID) -> Observation | None: ...

    async def list_observations(self, project_id: UUID) -> Sequence[Observation]: ...

    async def add_impact_analysis(self, impact: ImpactAnalysis) -> None: ...

    async def get_impact_analysis(self, impact_id: UUID) -> ImpactAnalysis | None: ...

    async def update_impact_analysis(self, impact: ImpactAnalysis) -> None: ...

    async def add_patch_set(self, patch_set: PatchSet) -> None: ...

    async def get_patch_set(self, patch_set_id: UUID) -> PatchSet | None: ...

    async def list_patch_sets(self, project_id: UUID) -> Sequence[PatchSet]: ...

    async def add_offer_snapshot(self, snapshot: OfferSnapshot) -> None: ...

    async def get_offer_snapshot(self, snapshot_id: UUID) -> OfferSnapshot | None: ...

    async def list_offer_snapshots(self, project_id: UUID) -> Sequence[OfferSnapshot]: ...

    async def add_purchase_proposal(self, proposal: PurchaseProposal) -> None: ...

    async def get_purchase_proposal(self, proposal_id: UUID) -> PurchaseProposal | None: ...

    async def list_purchase_proposals(self, project_id: UUID) -> Sequence[PurchaseProposal]: ...

    async def update_purchase_proposal(self, proposal: PurchaseProposal) -> None: ...

    async def add_checkout_handoff(self, handoff: CheckoutHandoff) -> None: ...

    async def get_checkout_handoff(self, handoff_id: UUID) -> CheckoutHandoff | None: ...

    async def list_checkout_handoffs(self, project_id: UUID) -> Sequence[CheckoutHandoff]: ...

    async def update_checkout_handoff(self, handoff: CheckoutHandoff) -> None: ...

    async def append_event(
        self,
        project_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> int: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
