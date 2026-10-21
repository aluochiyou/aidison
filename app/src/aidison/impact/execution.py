"""Durable deterministic execution for the first ImpactGraph vertical."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.application.service import DomainConflictError, DomainNotFoundError
from aidison.application.single_task_research import AgentRunCancelled
from aidison.domain.models import SolutionVersion
from aidison.impact.contracts import ImpactContract, ImpactReportManifest
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import session_scope
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ResearchResultStatus,
    ResultEnvelope,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim, AgentRunKind
from aidison.solution.contracts import SolutionChangeSet
from aidison.solution.impact import (
    build_impact_change_plan,
    build_steering_preview,
    traverse_solution_dependencies,
)


@dataclass(frozen=True, slots=True)
class ImpactRunExecution:
    """Only stable artifact and admission references leave the graph node."""

    structural_partition_ref: str
    change_plan_ref: str
    steering_preview_ref: str
    report_manifest: ImpactReportManifest
    result: ResultEnvelope
    admission: AdmissionRecord


class ImpactRunExecutor:
    """Execute a claimed Impact Run without a second scheduler or Domain write."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root

    async def execute(self, *, run: AgentRun, claim: AgentRunClaim) -> ImpactRunExecution:
        if run.kind is not AgentRunKind.IMPACT:
            raise DomainConflictError("claimed AgentRun is not an Impact run")
        await self._acknowledge_pending_cancellation(claim=claim)
        contract, change_set, solution = await self.load_frozen_inputs(run=run)
        partition = traverse_solution_dependencies(change_set=change_set, solution=solution)
        plan = build_impact_change_plan(change_set=change_set, partition=partition)
        preview = build_steering_preview(plan)

        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            partition_artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="impact_structural_partition",
                value=partition.model_dump(mode="json"),
            )
            plan_artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="impact_change_plan",
                value=plan.model_dump(mode="json"),
            )
            preview_artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="impact_steering_preview",
                value=preview.model_dump(mode="json"),
            )
            manifest = ImpactReportManifest(
                impact_run_id=run.id,
                basis_hash=run.basis_hash,
                impact_contract_ref=run.run_contract_ref or "",
                change_set_ref=contract.change_set_ref,
                structural_partition_ref=partition_artifact.ref,
                change_plan_ref=plan_artifact.ref,
                steering_preview_ref=preview_artifact.ref,
            )
            manifest_artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="impact_report_manifest",
                value=manifest.model_dump(mode="json"),
            )

        task_id = uuid5(NAMESPACE_URL, f"aidison://agent-run/{run.id}/impact.structural")
        result = ResultEnvelope(
            run_id=run.id,
            task_id=task_id,
            basis_hash=run.basis_hash,
            producer_attempt_id=claim.lease_token,
            producer_generation=claim.generation,
            producer_profile_ref=run.runtime_binding.profile_binding_ref,
            status=ResearchResultStatus.SUCCEEDED,
            artifact_ref=manifest_artifact.ref,
            manifest_hash=manifest_artifact.content_hash,
            evidence_refs=tuple(
                sorted(
                    {
                        evidence_ref
                        for path in partition.paths
                        for evidence_ref in path.evidence_refs
                    }
                )
            ),
            coverage_observation_refs=(),
            unresolved_refs=(),
        )
        admission = AdmissionRecord(
            run_id=run.id,
            result_id=result.id,
            result_manifest_hash=result.manifest_hash,
            disposition=AdmissionDisposition.ACCEPTED,
            reason_codes=(
                "runtime_fenced",
                "change_set_basis_valid",
                "typed_dependency_traversal",
                "artifact_present",
            ),
            admitted_ref=f"admitted://agent-run-results/{result.id}",
        )
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            if active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                raise AgentRunCancelled("ImpactRun cancellation acknowledged before admission")
            results = AgentResultStore(session)
            await results.record_result(result)
            await results.admit(admission)

        return ImpactRunExecution(
            structural_partition_ref=partition_artifact.ref,
            change_plan_ref=plan_artifact.ref,
            steering_preview_ref=preview_artifact.ref,
            report_manifest=manifest,
            result=result,
            admission=admission,
        )

    async def load_frozen_inputs(
        self, *, run: AgentRun
    ) -> tuple[ImpactContract, SolutionChangeSet, SolutionVersion]:
        if run.run_contract_ref is None or run.coverage_contract_ref is None:
            raise DomainConflictError("Impact AgentRun is missing its frozen contract references")
        async with session_scope(self._session_factory) as session:
            store = PostgresDomainStore(session)
            project = await store.get_project(run.project_id)
            if project is None:
                raise DomainNotFoundError("project not found")
            if project.revision != run.basis_project_revision:
                raise DomainConflictError("Impact AgentRun basis project revision is stale")
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            contract = ImpactContract.model_validate(
                await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=run.run_contract_ref,
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
            solution = await store.get_solution_version(contract.base_solution_version_id)
            if solution is None:
                raise DomainConflictError("Impact AgentRun base solution is unavailable")
            self._validate_contract(
                run=run, contract=contract, change_set=change_set, solution=solution
            )
            return contract, change_set, solution

    @staticmethod
    def _validate_contract(
        *,
        run: AgentRun,
        contract: ImpactContract,
        change_set: SolutionChangeSet,
        solution: SolutionVersion,
    ) -> None:
        if (
            contract.impact_run_id != run.id
            or contract.project_id != run.project_id
            or contract.basis_hash != run.basis_hash
            or contract.basis_project_revision != run.basis_project_revision
            or contract.coverage_contract_ref != run.coverage_contract_ref
        ):
            raise DomainConflictError("Impact contract does not match the claimed AgentRun")
        if not solution.dependency_projection_complete:
            raise DomainConflictError(
                "ImpactGraph requires a complete frozen dependency projection"
            )
        if (
            change_set.project_id != run.project_id
            or change_set.base_solution_version_id != solution.id
            or change_set.base_solution_basis_hash != solution.basis_hash
            or contract.base_solution_version_id != solution.id
            or contract.base_solution_basis_hash != solution.basis_hash
        ):
            raise DomainConflictError("Impact contract change set is stale")

    async def _acknowledge_pending_cancellation(self, *, claim: AgentRunClaim) -> None:
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            if active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                raise AgentRunCancelled("ImpactRun cancellation acknowledged before execution")


__all__ = ["ImpactRunExecution", "ImpactRunExecutor"]
