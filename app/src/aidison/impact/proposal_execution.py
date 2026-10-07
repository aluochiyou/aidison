"""Bounded semantic completion of an admitted deterministic Impact report."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.application.service import DomainConflictError
from aidison.application.single_task_research import AgentRunCancelled
from aidison.domain.models import Candidate, SolutionVersion
from aidison.impact.analyst import (
    ImpactAnalyst,
    ImpactPatchProposalPayload,
    validate_impact_patch_scope,
)
from aidison.impact.execution import ImpactRunExecution, ImpactRunExecutor
from aidison.impact.proposal import ImpactProposalManifest
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
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim
from aidison.solution.contracts import SolutionChangeSet
from aidison.solution.impact import traverse_solution_dependencies

_MAX_INSTRUCTION_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class ImpactProposalExecution:
    """Public immutable refs emitted before the graph interrupts for a user decision."""

    report: ImpactRunExecution
    raw_output_ref: str
    patch_candidate_ref: str
    proposal_manifest_ref: str
    result: ResultEnvelope
    admission: AdmissionRecord


class ImpactProposalExecutor:
    """Run one tool-free analyst call inside a deterministic frozen frontier.

    The analyst receives only bounded current candidates and frozen snapshots.
    It cannot create canonical facts: its candidate is schema- and scope-checked,
    then separated from the later user decision by ``ImpactProposalManifest``.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
        analyst: ImpactAnalyst,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._analyst = analyst
        self._report_executor = ImpactRunExecutor(
            session_factory=session_factory,
            artifact_root=artifact_root,
        )

    async def execute(self, *, run: AgentRun, claim: AgentRunClaim) -> ImpactProposalExecution:
        report = await self._report_executor.execute(run=run, claim=claim)
        change_set, solution, candidates = await self._load_analysis_material(run=run)
        partition = traverse_solution_dependencies(change_set=change_set, solution=solution)
        snapshots = {
            UUID(str(snapshot["module_id"])): str(snapshot["snapshot_hash"])
            for snapshot in solution.module_snapshots
        }
        instruction = _build_instruction(
            change_set=change_set,
            affected_module_ids=partition.affected_module_ids,
            snapshots_by_module_id=snapshots,
            candidates=candidates,
        )
        raw_json = await self._analyst.analyze(instruction=instruction)
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            raw = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="impact_raw_output",
                value={"raw_json": raw_json},
            )

        payload = ImpactPatchProposalPayload.model_validate_json(raw_json)
        payload = validate_impact_patch_scope(
            payload=payload,
            affected_module_ids=partition.affected_module_ids,
            snapshots_by_module_id=snapshots,
        )
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            candidate = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="impact_patch_candidate",
                value=payload.model_dump(mode="json"),
            )
        result = ResultEnvelope(
            run_id=run.id,
            task_id=uuid5(NAMESPACE_URL, f"aidison://agent-run/{run.id}/impact.analysis"),
            basis_hash=run.basis_hash,
            producer_attempt_id=claim.lease_token,
            producer_generation=claim.generation,
            producer_profile_ref=run.runtime_binding.profile_binding_ref,
            status=ResearchResultStatus.SUCCEEDED,
            artifact_ref=candidate.ref,
            manifest_hash=candidate.content_hash,
            evidence_refs=(),
            coverage_observation_refs=(),
            unresolved_refs=tuple(
                f"unknown://impact/{index}" for index, _ in enumerate(payload.unknowns)
            ),
        )
        admission = AdmissionRecord(
            run_id=run.id,
            result_id=result.id,
            result_manifest_hash=result.manifest_hash,
            disposition=AdmissionDisposition.ACCEPTED,
            reason_codes=(
                "runtime_fenced",
                "schema_valid",
                "frozen_impact_frontier",
                "raw_output_audited",
                "artifact_present",
            ),
            admitted_ref=f"admitted://agent-run-results/{result.id}",
        )
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            if active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                raise AgentRunCancelled(
                    "ImpactRun cancellation acknowledged before proposal admission"
                )
            results = AgentResultStore(session)
            await results.record_result(result)
            await results.admit(admission)
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            raw_hash, _ = ContentAddressedArtifactStore.parse_ref(raw.ref)
            proposal = ImpactProposalManifest(
                run_id=run.id,
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                impact_contract_ref=run.run_contract_ref or "",
                impact_report_manifest_ref=report.result.artifact_ref,
                impact_report_manifest_hash=report.result.manifest_hash,
                raw_output_ref=raw.ref,
                raw_output_hash=raw_hash,
                patch_candidate_ref=candidate.ref,
                patch_candidate_hash=candidate.content_hash,
            )
            manifest = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="impact_proposal_manifest",
                value=proposal.model_dump(mode="json"),
            )
        return ImpactProposalExecution(
            report=report,
            raw_output_ref=raw.ref,
            patch_candidate_ref=candidate.ref,
            proposal_manifest_ref=manifest.ref,
            result=result,
            admission=admission,
        )

    async def _load_analysis_material(
        self, *, run: AgentRun
    ) -> tuple[SolutionChangeSet, SolutionVersion, tuple[Candidate, ...]]:
        contract, change_set, solution = await self._report_executor.load_frozen_inputs(run=run)
        if contract.impact_run_id != run.id:  # pragma: no cover - contract validation above
            raise DomainConflictError("Impact contract does not belong to this run")
        async with session_scope(self._session_factory) as session:
            store = PostgresDomainStore(session)
            candidates = tuple(
                item
                for item in await store.list_candidates(run.project_id)
                if item.module_id in set(
                    traverse_solution_dependencies(
                        change_set=change_set, solution=solution
                    ).affected_module_ids
                )
            )
        if not candidates:
            raise DomainConflictError(
                "Impact Analyst requires at least one candidate in its frontier"
            )
        return change_set, solution, candidates


def _build_instruction(
    *,
    change_set: SolutionChangeSet,
    affected_module_ids: tuple[UUID, ...],
    snapshots_by_module_id: dict[UUID, str],
    candidates: tuple[Candidate, ...],
) -> str:
    """Build bounded model input without passing mutable project or hidden state."""

    candidate_lines = "\n".join(
        "- module_id={module_id}; candidate_id={candidate_id}; name={name}; "
        "evidence_binding_ids={evidence_ids}".format(
            module_id=item.module_id,
            candidate_id=item.id,
            name=item.name,
            evidence_ids=",".join(str(value) for value in item.evidence_binding_ids),
        )
        for item in sorted(candidates, key=lambda item: (str(item.module_id), str(item.id)))
    )
    module_lines = "\n".join(
        f"- module_id={module_id}; base_snapshot_hash={snapshots_by_module_id[module_id]}"
        for module_id in affected_module_ids
    )
    instruction = f"""Prepare one minimal impact patch candidate.
Frozen trigger: {change_set.trigger_ref}
Allowed affected modules and immutable base snapshots:
{module_lines}
Permitted existing candidates (do not invent identifiers):
{candidate_lines}
Return a patch only for modules that actually need a replacement. Implementation and
verification steps may cover any affected module. Every patch must carry its exact
base_snapshot_hash. Use unknowns for missing facts and do not claim execution."""
    return instruction[:_MAX_INSTRUCTION_CHARS]


__all__ = ["ImpactProposalExecution", "ImpactProposalExecutor"]
