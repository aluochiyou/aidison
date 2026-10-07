"""Canonical ImpactAnalysis / PatchSet / SolutionVersion write after user approval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aidison.application.service import (
    DomainConflictError,
    DomainNotFoundError,
    PreconditionFailedError,
    ProjectApplication,
)
from aidison.domain.models import (
    BomItem,
    ImpactAnalysis,
    ModulePatch,
    ModuleSelection,
    PatchSet,
    SolutionPlanStep,
    SolutionVersion,
)
from aidison.impact.analyst import ImpactPatchProposalPayload
from aidison.impact.contracts import ImpactContract
from aidison.impact.proposal import ImpactProposalManifest
from aidison.infrastructure.agent_decisions import AgentRunDecisionStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.decision_contracts import AgentRunDecisionStatus
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.solution.contracts import SolutionChangeSet


@dataclass(frozen=True, slots=True)
class ImpactProposalCommitResult:
    """Canonical facts produced only by an approved human decision."""

    impact: ImpactAnalysis
    patch_set: PatchSet
    solution: SolutionVersion


class ImpactProposalCommitApplication:
    """The sole v2 path that maps a bounded analyst candidate into Domain commands."""

    def __init__(self, session: AsyncSession, *, artifact_root: Path) -> None:
        self._session = session
        self._store = PostgresDomainStore(session)
        self._artifact_root = artifact_root

    async def commit_approved_decision(
        self,
        *,
        agent_run_decision_id: UUID,
        expected_project_revision: int,
    ) -> ImpactProposalCommitResult | None:
        agent_decision = await AgentRunDecisionStore(self._session).get(agent_run_decision_id)
        if agent_decision is None:
            raise DomainNotFoundError("AgentRun decision not found")
        control = AgentRunControl(self._session)
        run = await control.get(agent_decision.agent_run_id)
        if run is None or run.kind is not AgentRunKind.IMPACT:
            raise DomainConflictError("AgentRun decision is not an ImpactGraph decision")
        if agent_decision.status is AgentRunDecisionStatus.REJECTED:
            if run.status is AgentRunStatus.WAITING:
                await control.complete_waiting_run(run_id=run.id, succeeded=False)
                await self._session.commit()
            return None
        if agent_decision.status is not AgentRunDecisionStatus.APPROVED:
            raise DomainConflictError("Impact proposal still requires user decision")

        project = await self._store.get_project(run.project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        manifest, contract, change_set, payload = await self._load_review_material(
            run=run,
            ref=agent_decision.proposal_manifest_ref,
        )
        if contract.impact_run_id != run.id:
            raise DomainConflictError("Impact proposal no longer matches its frozen run")
        patches = tuple(
            ModulePatch(
                module_id=item.module_id,
                base_snapshot_hash=item.base_snapshot_hash,
                replacement=ModuleSelection(
                    module_id=item.module_id,
                    candidate_id=item.candidate_id,
                    candidate_name=item.candidate_name,
                    rationale=item.rationale,
                    evidence_binding_ids=item.evidence_binding_ids,
                    risks=item.risks,
                ),
            )
            for item in payload.module_patches
        )
        bom = tuple(
            BomItem(
                line_id=item.line_id,
                module_id=item.module_id,
                candidate_id=item.candidate_id,
                name=item.name,
                quantity=item.quantity,
                unit=item.unit,
                evidence_binding_ids=item.evidence_binding_ids,
            )
            for item in payload.replacement_bom_items
        )
        implementation = tuple(
            SolutionPlanStep(
                step_id=item.step_id,
                title=item.title,
                instruction=item.instruction,
                module_ids=item.module_ids,
                acceptance=item.acceptance,
            )
            for item in payload.replacement_implementation_steps
        )
        verification = tuple(
            SolutionPlanStep(
                step_id=item.step_id,
                title=item.title,
                instruction=item.instruction,
                module_ids=item.module_ids,
                acceptance=item.acceptance,
            )
            for item in payload.replacement_verification_steps
        )
        domain = ProjectApplication(self._store)
        impact = await domain.submit_impact_analysis(
            project_id=project.id,
            expected_project_revision=expected_project_revision,
            observation_id=change_set.change_set_id,
            module_patches=patches,
            stale_evidence_binding_ids=payload.stale_evidence_binding_ids,
            replacement_bom_items=bom,
            replacement_implementation_steps=implementation,
            replacement_verification_steps=verification,
            summary=payload.summary,
            risks=payload.risks,
            artifact_ref=agent_decision.proposal_manifest_ref,
            profile_id="langgraph-impact-analyst",
            profile_revision=1,
            idempotency_key=f"impact-agent-run-proposal:{run.id}",
        )
        patch_set, solution = await domain.approve_impact_and_patch(
            impact_id=impact.id,
            expected_project_revision=expected_project_revision + 1,
            basis_hash=impact.basis_hash,
            idempotency_key=f"impact-agent-run-approve:{run.id}",
        )
        current_run = await control.get(run.id)
        if current_run is not None and current_run.status is AgentRunStatus.WAITING:
            await control.complete_waiting_run(run_id=run.id, succeeded=True)
        elif current_run is None or current_run.status is not AgentRunStatus.SUCCEEDED:
            raise DomainConflictError("Impact AgentRun is not waiting for decision completion")
        await self._session.commit()
        approved_impact = await self._store.get_impact_analysis(impact.id)
        if approved_impact is None:  # pragma: no cover - Domain write above is transactional
            raise DomainConflictError("approved ImpactAnalysis is unavailable")
        return ImpactProposalCommitResult(
            impact=approved_impact,
            patch_set=patch_set,
            solution=solution,
        )

    async def _load_review_material(
        self,
        *,
        run: AgentRun,
        ref: str,
    ) -> tuple[
        ImpactProposalManifest,
        ImpactContract,
        SolutionChangeSet,
        ImpactPatchProposalPayload,
    ]:
        artifacts = ContentAddressedArtifactStore(self._session, self._artifact_root)
        manifest = ImpactProposalManifest.model_validate(
            await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=ref,
                expected_kind="impact_proposal_manifest",
            )
        )
        if (
            manifest.run_id != run.id
            or manifest.project_id != run.project_id
            or manifest.basis_hash != run.basis_hash
            or manifest.impact_contract_ref != run.run_contract_ref
            or not manifest.can_create_patch
        ):
            raise DomainConflictError("Impact Proposal Manifest is not READY for commit")
        report_hash, _ = ContentAddressedArtifactStore.parse_ref(
            manifest.impact_report_manifest_ref
        )
        raw_hash, _ = ContentAddressedArtifactStore.parse_ref(manifest.raw_output_ref)
        if (
            report_hash != manifest.impact_report_manifest_hash
            or raw_hash != manifest.raw_output_hash
        ):
            raise DomainConflictError("Impact proposal audit Artifact hash is stale")
        contract = ImpactContract.model_validate(
            await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=manifest.impact_contract_ref,
                expected_kind="impact_contract",
            )
        )
        change_set = SolutionChangeSet.model_validate(
            await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=contract.change_set_ref,
                expected_kind="solution_change_set",
            )
        )
        candidate_hash, _ = ContentAddressedArtifactStore.parse_ref(manifest.patch_candidate_ref)
        if candidate_hash != manifest.patch_candidate_hash:
            raise DomainConflictError("Impact patch candidate hash is stale")
        payload = ImpactPatchProposalPayload.model_validate(
            await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=manifest.patch_candidate_ref,
                expected_kind="impact_patch_candidate",
            )
        )
        return manifest, contract, change_set, payload


__all__ = ["ImpactProposalCommitApplication", "ImpactProposalCommitResult"]
