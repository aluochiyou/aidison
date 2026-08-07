from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar, cast
from uuid import UUID

import sqlalchemy
from pydantic import BaseModel
from sqlalchemy import CursorResult, Select, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.application.ports import (
    DomainStore,
    DuplicateCommandError,
    OptimisticConcurrencyError,
)
from aidison.domain.models import (
    Candidate,
    CheckoutHandoff,
    CompatibilityFinding,
    DecisionRequest,
    DecisionStatus,
    EffectApproval,
    EffectApprovalStatus,
    EvidenceBinding,
    ImpactAnalysis,
    ImpactStatus,
    Module,
    Observation,
    OfferSnapshot,
    PatchSet,
    Project,
    ProjectStage,
    PurchaseProposal,
    RequirementRevision,
    SolutionProposal,
    SolutionProposalStatus,
    SolutionVersion,
)
from aidison.infrastructure.orm import (
    CandidateRow,
    CheckoutHandoffRow,
    CommandReceiptRow,
    CompatibilityFindingRow,
    DecisionRequestRow,
    DomainEventRow,
    EffectApprovalRow,
    EvidenceBindingRow,
    ImpactAnalysisRow,
    ModuleRow,
    ObservationRow,
    OfferSnapshotRow,
    PatchSetRow,
    ProjectRow,
    PurchaseProposalRow,
    RequirementRevisionRow,
    SolutionProposalRow,
    SolutionVersionRow,
)
from aidison.infrastructure.signals import publish_domain_event_signal

ModelT = TypeVar("ModelT", bound=BaseModel)


class PersistenceCorruptionError(RuntimeError):
    """Raised when typed identity columns disagree with the canonical JSON payload."""


def _payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _decode(model_type: type[ModelT], payload: dict[str, Any]) -> ModelT:
    return model_type.model_validate(payload)


def _assert_identity(
    *,
    entity_name: str,
    row_id: UUID,
    row_project_id: UUID,
    payload_id: UUID,
    payload_project_id: UUID,
) -> None:
    if row_id != payload_id or row_project_id != payload_project_id:
        raise PersistenceCorruptionError(f"{entity_name} typed identity differs from payload")


class PostgresDomainStore(DomainStore):
    """Transactional PostgreSQL implementation of Aidison's canonical Domain store.

    One instance is bound to one request/session transaction. `claim_command`
    holds a transaction-scoped advisory lock, so equal idempotency keys cannot
    both execute before the winner commits or rolls back.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_command(self, idempotency_key: str, payload_hash: str) -> str | None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": idempotency_key},
        )
        receipt = await self._session.get(CommandReceiptRow, idempotency_key)
        if receipt is None:
            return None
        if receipt.payload_hash != payload_hash:
            raise DuplicateCommandError("idempotency key was used with another payload")
        return receipt.result_ref

    async def save_command_receipt(
        self,
        idempotency_key: str,
        payload_hash: str,
        result_ref: str,
    ) -> None:
        self._session.add(
            CommandReceiptRow(
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                result_ref=result_ref,
            )
        )

    async def add_project(self, project: Project) -> None:
        self._session.add(
            ProjectRow(
                id=project.id,
                name=project.name,
                goal=project.goal,
                stage=project.stage.value,
                revision=project.revision,
                event_sequence=0,
                active_requirement_revision_id=project.active_requirement_revision_id,
                active_solution_version_id=project.active_solution_version_id,
                created_at=project.created_at,
                updated_at=project.updated_at,
            )
        )

    async def get_project(self, project_id: UUID) -> Project | None:
        row = await self._session.get(ProjectRow, project_id)
        if row is None:
            return None
        return self._project_from_row(row)

    async def list_projects(self, *, limit: int = 50) -> Sequence[Project]:
        rows = (
            await self._session.scalars(
                select(ProjectRow).order_by(ProjectRow.updated_at.desc()).limit(limit)
            )
        ).all()
        return tuple(self._project_from_row(row) for row in rows)

    @staticmethod
    def _project_from_row(row: ProjectRow) -> Project:
        return Project(
            id=row.id,
            name=row.name,
            goal=row.goal,
            stage=ProjectStage(row.stage),
            revision=row.revision,
            active_requirement_revision_id=row.active_requirement_revision_id,
            active_solution_version_id=row.active_solution_version_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def update_project(self, project: Project, *, expected_revision: int) -> None:
        statement = (
            update(ProjectRow)
            .where(ProjectRow.id == project.id, ProjectRow.revision == expected_revision)
            .values(
                name=project.name,
                goal=project.goal,
                stage=project.stage.value,
                revision=project.revision,
                active_requirement_revision_id=project.active_requirement_revision_id,
                active_solution_version_id=project.active_solution_version_id,
                updated_at=project.updated_at,
            )
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("project revision is stale")

    async def add_requirement_revision(self, requirement: RequirementRevision) -> None:
        self._session.add(
            RequirementRevisionRow(
                id=requirement.id,
                project_id=requirement.project_id,
                revision=requirement.revision,
                payload=_payload(requirement),
            )
        )
        await self._session.flush()

    async def get_requirement_revision(
        self,
        requirement_id: UUID,
    ) -> RequirementRevision | None:
        row = await self._session.get(RequirementRevisionRow, requirement_id)
        if row is None:
            return None
        requirement = _decode(RequirementRevision, row.payload)
        _assert_identity(
            entity_name="RequirementRevision",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=requirement.id,
            payload_project_id=requirement.project_id,
        )
        return requirement

    async def list_requirement_revisions(
        self,
        project_id: UUID,
    ) -> Sequence[RequirementRevision]:
        statement = (
            select(RequirementRevisionRow)
            .where(RequirementRevisionRow.project_id == project_id)
            .order_by(RequirementRevisionRow.revision)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(RequirementRevision, row.payload) for row in rows]

    async def add_modules(self, modules: Sequence[Module]) -> None:
        self._session.add_all(
            [
                ModuleRow(
                    id=module.id,
                    project_id=module.project_id,
                    requirement_revision_id=module.requirement_revision_id,
                    key=module.key,
                    payload=_payload(module),
                )
                for module in modules
            ]
        )

    async def list_modules(
        self,
        project_id: UUID,
        requirement_revision_id: UUID | None = None,
    ) -> Sequence[Module]:
        statement: Select[tuple[ModuleRow]] = select(ModuleRow).where(
            ModuleRow.project_id == project_id
        )
        if requirement_revision_id is not None:
            statement = statement.where(
                ModuleRow.requirement_revision_id == requirement_revision_id
            )
        rows = (await self._session.scalars(statement.order_by(ModuleRow.key))).all()
        return [_decode(Module, row.payload) for row in rows]

    async def add_evidence_bindings(self, bindings: Sequence[EvidenceBinding]) -> None:
        self._session.add_all(
            [
                EvidenceBindingRow(
                    id=binding.id,
                    project_id=binding.project_id,
                    module_id=binding.module_id,
                    payload=_payload(binding),
                )
                for binding in bindings
            ]
        )

    async def list_evidence_bindings(self, project_id: UUID) -> Sequence[EvidenceBinding]:
        statement = (
            select(EvidenceBindingRow)
            .where(EvidenceBindingRow.project_id == project_id)
            .order_by(EvidenceBindingRow.id)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(EvidenceBinding, row.payload) for row in rows]

    async def add_candidates(self, candidates: Sequence[Candidate]) -> None:
        self._session.add_all(
            [
                CandidateRow(
                    id=candidate.id,
                    project_id=candidate.project_id,
                    module_id=candidate.module_id,
                    payload=_payload(candidate),
                )
                for candidate in candidates
            ]
        )

    async def list_candidates(self, project_id: UUID) -> Sequence[Candidate]:
        statement = (
            select(CandidateRow)
            .where(CandidateRow.project_id == project_id)
            .order_by(CandidateRow.id)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(Candidate, row.payload) for row in rows]

    async def add_compatibility_findings(
        self,
        findings: Sequence[CompatibilityFinding],
    ) -> None:
        self._session.add_all(
            [
                CompatibilityFindingRow(
                    id=finding.id,
                    project_id=finding.project_id,
                    payload=_payload(finding),
                )
                for finding in findings
            ]
        )

    async def list_compatibility_findings(
        self,
        project_id: UUID,
    ) -> Sequence[CompatibilityFinding]:
        statement = (
            select(CompatibilityFindingRow)
            .where(CompatibilityFindingRow.project_id == project_id)
            .order_by(CompatibilityFindingRow.id)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(CompatibilityFinding, row.payload) for row in rows]

    async def add_decision_request(self, decision: DecisionRequest) -> None:
        self._session.add(
            DecisionRequestRow(
                id=decision.id,
                project_id=decision.project_id,
                status=decision.status.value,
                basis_hash=decision.basis_hash,
                payload=_payload(decision),
            )
        )

    async def get_decision_request(self, decision_id: UUID) -> DecisionRequest | None:
        row = await self._session.get(DecisionRequestRow, decision_id)
        if row is None:
            return None
        decision = _decode(DecisionRequest, row.payload)
        _assert_identity(
            entity_name="DecisionRequest",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=decision.id,
            payload_project_id=decision.project_id,
        )
        if row.status != decision.status.value or row.basis_hash != decision.basis_hash:
            raise PersistenceCorruptionError("DecisionRequest typed state differs from payload")
        return decision

    async def update_decision_request(self, decision: DecisionRequest) -> None:
        statement = (
            update(DecisionRequestRow)
            .where(
                DecisionRequestRow.id == decision.id,
                DecisionRequestRow.status == DecisionStatus.PENDING.value,
                DecisionRequestRow.basis_hash == decision.basis_hash,
            )
            .values(status=decision.status.value, payload=_payload(decision))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("decision is stale or already resolved")

    async def add_solution_proposal(self, proposal: SolutionProposal) -> None:
        self._session.add(
            SolutionProposalRow(
                id=proposal.id,
                project_id=proposal.project_id,
                decision_id=proposal.decision_id,
                requirement_revision_id=proposal.requirement_revision_id,
                status=proposal.status.value,
                basis_hash=proposal.basis_hash,
                payload=_payload(proposal),
            )
        )

    async def get_solution_proposal(self, proposal_id: UUID) -> SolutionProposal | None:
        row = await self._session.get(SolutionProposalRow, proposal_id)
        if row is None:
            return None
        proposal = _decode(SolutionProposal, row.payload)
        _assert_identity(
            entity_name="SolutionProposal",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=proposal.id,
            payload_project_id=proposal.project_id,
        )
        if row.status != proposal.status.value or row.basis_hash != proposal.basis_hash:
            raise PersistenceCorruptionError("SolutionProposal typed state differs from payload")
        return proposal

    async def list_solution_proposals(self, project_id: UUID) -> Sequence[SolutionProposal]:
        rows = (
            await self._session.scalars(
                select(SolutionProposalRow)
                .where(SolutionProposalRow.project_id == project_id)
                .order_by(SolutionProposalRow.id)
            )
        ).all()
        return [_decode(SolutionProposal, row.payload) for row in rows]

    async def update_solution_proposal(self, proposal: SolutionProposal) -> None:
        statement = (
            update(SolutionProposalRow)
            .where(
                SolutionProposalRow.id == proposal.id,
                SolutionProposalRow.status == SolutionProposalStatus.PROPOSED.value,
                SolutionProposalRow.basis_hash == proposal.basis_hash,
            )
            .values(status=proposal.status.value, payload=_payload(proposal))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("solution proposal is stale or already resolved")

    async def add_solution_version(self, solution: SolutionVersion) -> None:
        self._session.add(
            SolutionVersionRow(
                id=solution.id,
                project_id=solution.project_id,
                version=solution.version,
                requirement_revision_id=solution.requirement_revision_id,
                approved_decision_id=solution.approved_decision_id,
                solution_proposal_id=solution.solution_proposal_id,
                previous_version_id=solution.previous_version_id,
                basis_hash=solution.basis_hash,
                payload=_payload(solution),
            )
        )

    async def get_solution_version(self, solution_id: UUID) -> SolutionVersion | None:
        row = await self._session.get(SolutionVersionRow, solution_id)
        if row is None:
            return None
        solution = _decode(SolutionVersion, row.payload)
        _assert_identity(
            entity_name="SolutionVersion",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=solution.id,
            payload_project_id=solution.project_id,
        )
        if row.version != solution.version or row.basis_hash != solution.basis_hash:
            raise PersistenceCorruptionError("SolutionVersion typed state differs from payload")
        return solution

    async def list_solution_versions(self, project_id: UUID) -> Sequence[SolutionVersion]:
        statement = (
            select(SolutionVersionRow)
            .where(SolutionVersionRow.project_id == project_id)
            .order_by(SolutionVersionRow.version)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(SolutionVersion, row.payload) for row in rows]

    async def add_observation(self, observation: Observation) -> None:
        self._session.add(
            ObservationRow(
                id=observation.id,
                project_id=observation.project_id,
                solution_version_id=observation.solution_version_id,
                payload=_payload(observation),
            )
        )
        await self._session.flush()

    async def get_observation(self, observation_id: UUID) -> Observation | None:
        row = await self._session.get(ObservationRow, observation_id)
        if row is None:
            return None
        observation = _decode(Observation, row.payload)
        _assert_identity(
            entity_name="Observation",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=observation.id,
            payload_project_id=observation.project_id,
        )
        return observation

    async def list_observations(self, project_id: UUID) -> Sequence[Observation]:
        statement = (
            select(ObservationRow)
            .where(ObservationRow.project_id == project_id)
            .order_by(ObservationRow.id)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(Observation, row.payload) for row in rows]

    async def add_impact_analysis(self, impact: ImpactAnalysis) -> None:
        self._session.add(
            ImpactAnalysisRow(
                id=impact.id,
                project_id=impact.project_id,
                status=impact.status.value,
                observation_id=impact.observation_id,
                base_solution_version_id=impact.base_solution_version_id,
                basis_hash=impact.basis_hash,
                payload=_payload(impact),
            )
        )

    async def get_impact_analysis(self, impact_id: UUID) -> ImpactAnalysis | None:
        row = await self._session.get(ImpactAnalysisRow, impact_id)
        if row is None:
            return None
        impact = _decode(ImpactAnalysis, row.payload)
        _assert_identity(
            entity_name="ImpactAnalysis",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=impact.id,
            payload_project_id=impact.project_id,
        )
        if row.status != impact.status.value or row.basis_hash != impact.basis_hash:
            raise PersistenceCorruptionError("ImpactAnalysis typed state differs from payload")
        return impact

    async def update_impact_analysis(self, impact: ImpactAnalysis) -> None:
        statement = (
            update(ImpactAnalysisRow)
            .where(
                ImpactAnalysisRow.id == impact.id,
                ImpactAnalysisRow.status == ImpactStatus.PROPOSED.value,
                ImpactAnalysisRow.basis_hash == impact.basis_hash,
            )
            .values(status=impact.status.value, payload=_payload(impact))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("impact is stale or already resolved")

    async def add_patch_set(self, patch_set: PatchSet) -> None:
        self._session.add(
            PatchSetRow(
                id=patch_set.id,
                project_id=patch_set.project_id,
                impact_analysis_id=patch_set.impact_analysis_id,
                base_solution_version_id=patch_set.base_solution_version_id,
                payload=_payload(patch_set),
            )
        )

    async def get_patch_set(self, patch_set_id: UUID) -> PatchSet | None:
        row = await self._session.get(PatchSetRow, patch_set_id)
        if row is None:
            return None
        patch_set = _decode(PatchSet, row.payload)
        _assert_identity(
            entity_name="PatchSet",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=patch_set.id,
            payload_project_id=patch_set.project_id,
        )
        return patch_set

    async def list_patch_sets(self, project_id: UUID) -> Sequence[PatchSet]:
        rows = (
            await self._session.scalars(
                select(PatchSetRow)
                .where(PatchSetRow.project_id == project_id)
                .order_by(PatchSetRow.id)
            )
        ).all()
        return [_decode(PatchSet, row.payload) for row in rows]

    # ── V1 Shopping ────────────────────────────────────────────────────

    async def add_offer_snapshot(self, snapshot: OfferSnapshot) -> None:
        self._session.add(
            OfferSnapshotRow(
                id=snapshot.id,
                project_id=snapshot.project_id,
                solution_version_id=snapshot.solution_version_id,
                bom_line_id=snapshot.bom_line_id,
                provider=snapshot.provider,
                provider_offer_id=snapshot.provider_offer_id,
                snapshot_hash=snapshot.snapshot_hash,
                payload=_payload(snapshot),
                observed_at=snapshot.observed_at,
            )
        )

    async def get_offer_snapshot(self, snapshot_id: UUID) -> OfferSnapshot | None:
        row = await self._session.get(OfferSnapshotRow, snapshot_id)
        if row is None:
            return None
        snapshot = _decode(OfferSnapshot, row.payload)
        _assert_identity(
            entity_name="OfferSnapshot",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=snapshot.id,
            payload_project_id=snapshot.project_id,
        )
        return snapshot

    async def list_offer_snapshots(self, project_id: UUID) -> Sequence[OfferSnapshot]:
        statement = (
            select(OfferSnapshotRow)
            .where(OfferSnapshotRow.project_id == project_id)
            .order_by(OfferSnapshotRow.observed_at.desc())
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(OfferSnapshot, row.payload) for row in rows]

    async def add_purchase_proposal(self, proposal: PurchaseProposal) -> None:
        self._session.add(
            PurchaseProposalRow(
                id=proposal.id,
                project_id=proposal.project_id,
                solution_version_id=proposal.solution_version_id,
                offer_snapshot_id=proposal.offer_snapshot_id,
                status=proposal.status.value,
                basis_hash=proposal.basis_hash,
                payload=_payload(proposal),
            )
        )

    async def get_purchase_proposal(self, proposal_id: UUID) -> PurchaseProposal | None:
        row = await self._session.get(PurchaseProposalRow, proposal_id)
        if row is None:
            return None
        proposal = _decode(PurchaseProposal, row.payload)
        _assert_identity(
            entity_name="PurchaseProposal",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=proposal.id,
            payload_project_id=proposal.project_id,
        )
        if row.status != proposal.status.value or row.basis_hash != proposal.basis_hash:
            raise PersistenceCorruptionError("PurchaseProposal typed state differs from payload")
        return proposal

    async def list_purchase_proposals(self, project_id: UUID) -> Sequence[PurchaseProposal]:
        rows = (
            await self._session.scalars(
                select(PurchaseProposalRow)
                .where(PurchaseProposalRow.project_id == project_id)
                .order_by(PurchaseProposalRow.created_at.desc())
            )
        ).all()
        return [_decode(PurchaseProposal, row.payload) for row in rows]

    async def update_purchase_proposal(self, proposal: PurchaseProposal) -> None:
        statement = (
            sqlalchemy.update(PurchaseProposalRow)
            .where(
                PurchaseProposalRow.id == proposal.id,
                PurchaseProposalRow.basis_hash == proposal.basis_hash,
            )
            .values(status=proposal.status.value, payload=_payload(proposal))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("purchase proposal is stale or already updated")

    async def add_effect_approval(self, approval: EffectApproval) -> None:
        self._session.add(
            EffectApprovalRow(
                id=approval.id,
                project_id=approval.project_id,
                effect_kind=approval.effect_kind,
                target_ref=approval.target_ref,
                basis_hash=approval.basis_hash,
                scope_hash=approval.scope_hash,
                constraints=approval.constraints,
                status=approval.status.value,
                payload=_payload(approval),
                requested_at=approval.requested_at,
                expires_at=approval.expires_at,
                resolved_at=approval.resolved_at,
                consumed_at=approval.consumed_at,
            )
        )

    async def get_effect_approval(self, approval_id: UUID) -> EffectApproval | None:
        row = await self._session.get(EffectApprovalRow, approval_id)
        if row is None:
            return None
        approval = _decode(EffectApproval, row.payload)
        _assert_identity(
            entity_name="EffectApproval",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=approval.id,
            payload_project_id=approval.project_id,
        )
        if (
            row.effect_kind != approval.effect_kind
            or row.target_ref != approval.target_ref
            or row.basis_hash != approval.basis_hash
            or row.scope_hash != approval.scope_hash
            or row.constraints != approval.constraints
            or row.status != approval.status.value
            or row.requested_at != approval.requested_at
            or row.expires_at != approval.expires_at
            or row.resolved_at != approval.resolved_at
            or row.consumed_at != approval.consumed_at
        ):
            raise PersistenceCorruptionError("EffectApproval typed state differs from payload")
        return approval

    async def list_effect_approvals(self, project_id: UUID) -> Sequence[EffectApproval]:
        rows = (
            await self._session.scalars(
                select(EffectApprovalRow)
                .where(EffectApprovalRow.project_id == project_id)
                .order_by(EffectApprovalRow.requested_at.desc(), EffectApprovalRow.id)
            )
        ).all()
        return [_decode(EffectApproval, row.payload) for row in rows]

    async def find_live_effect_approval(
        self,
        project_id: UUID,
        scope_hash: str,
    ) -> EffectApproval | None:
        row = await self._session.scalar(
            select(EffectApprovalRow)
            .where(
                EffectApprovalRow.project_id == project_id,
                EffectApprovalRow.scope_hash == scope_hash,
                EffectApprovalRow.status.in_(
                    (
                        EffectApprovalStatus.REQUESTED.value,
                        EffectApprovalStatus.APPROVED.value,
                    )
                ),
            )
            .with_for_update()
        )
        return None if row is None else _decode(EffectApproval, row.payload)

    async def update_effect_approval(
        self,
        approval: EffectApproval,
        *,
        expected_status: EffectApprovalStatus,
    ) -> None:
        statement = (
            sqlalchemy.update(EffectApprovalRow)
            .where(
                EffectApprovalRow.id == approval.id,
                EffectApprovalRow.project_id == approval.project_id,
                EffectApprovalRow.effect_kind == approval.effect_kind,
                EffectApprovalRow.target_ref == approval.target_ref,
                EffectApprovalRow.basis_hash == approval.basis_hash,
                EffectApprovalRow.scope_hash == approval.scope_hash,
                EffectApprovalRow.status == expected_status.value,
            )
            .values(
                status=approval.status.value,
                payload=_payload(approval),
                resolved_at=approval.resolved_at,
                consumed_at=approval.consumed_at,
            )
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("effect approval is stale or terminal")

    async def add_checkout_handoff(self, handoff: CheckoutHandoff) -> None:
        self._session.add(
            CheckoutHandoffRow(
                id=handoff.id,
                project_id=handoff.project_id,
                proposal_id=handoff.proposal_id,
                provider=handoff.provider,
                status=handoff.status.value,
                basis_hash=handoff.basis_hash,
                payload=_payload(handoff),
            )
        )

    async def get_checkout_handoff(self, handoff_id: UUID) -> CheckoutHandoff | None:
        row = await self._session.get(CheckoutHandoffRow, handoff_id)
        if row is None:
            return None
        handoff = _decode(CheckoutHandoff, row.payload)
        _assert_identity(
            entity_name="CheckoutHandoff",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=handoff.id,
            payload_project_id=handoff.project_id,
        )
        if row.status != handoff.status.value or row.basis_hash != handoff.basis_hash:
            raise PersistenceCorruptionError("CheckoutHandoff typed state differs from payload")
        return handoff

    async def list_checkout_handoffs(self, project_id: UUID) -> Sequence[CheckoutHandoff]:
        rows = (
            await self._session.scalars(
                select(CheckoutHandoffRow)
                .where(CheckoutHandoffRow.project_id == project_id)
                .order_by(CheckoutHandoffRow.created_at.desc())
            )
        ).all()
        return [_decode(CheckoutHandoff, row.payload) for row in rows]

    async def update_checkout_handoff(self, handoff: CheckoutHandoff) -> None:
        statement = (
            sqlalchemy.update(CheckoutHandoffRow)
            .where(CheckoutHandoffRow.id == handoff.id)
            .values(status=handoff.status.value, payload=_payload(handoff))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("checkout handoff is stale or missing")

    async def append_event(
        self,
        project_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> int:
        sequence_statement = (
            update(ProjectRow)
            .where(ProjectRow.id == project_id)
            .values(event_sequence=ProjectRow.event_sequence + 1)
            .returning(ProjectRow.event_sequence)
        )
        project_sequence = await self._session.scalar(sequence_statement)
        if project_sequence is None:
            raise OptimisticConcurrencyError("project disappeared while appending event")
        self._session.add(
            DomainEventRow(
                project_id=project_id,
                project_seq=project_sequence,
                event_type=event_type,
                payload=dict(payload),
            )
        )
        await publish_domain_event_signal(
            self._session,
            project_id=project_id,
            project_sequence=project_sequence,
            event_type=event_type,
        )
        return project_sequence

    async def list_events(
        self,
        project_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> Sequence[DomainEventRow]:
        statement = (
            select(DomainEventRow)
            .where(
                DomainEventRow.project_id == project_id,
                DomainEventRow.project_seq > after_sequence,
            )
            .order_by(DomainEventRow.project_seq)
            .limit(limit)
        )
        return (await self._session.scalars(statement)).all()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
