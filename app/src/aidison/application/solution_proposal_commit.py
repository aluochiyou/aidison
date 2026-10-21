"""Canonical SolutionVersion commit for an approved ready Solution Proposal Manifest."""

from __future__ import annotations

import re
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
    DecisionStatus,
    ModuleSelection,
    SolutionPlanStep,
    SolutionVersion,
)
from aidison.infrastructure.agent_decisions import AgentRunDecisionStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.decision_contracts import AgentRunDecisionStatus
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.solution.dependencies import derive_solution_dependencies
from aidison.solution.elements import SolutionElementSet
from aidison.solution.proposal_manifest import SolutionProposalManifest

_DECISION_REF = re.compile(r"^decision://([0-9a-fA-F-]{36})/option/[a-z][a-z0-9_-]{1,63}$")
_CANDIDATE_REF = re.compile(r"^candidate://([0-9a-fA-F-]{36})$")
_EVIDENCE_REF = re.compile(r"^evidence-binding://([0-9a-fA-F-]{36})$")


def _ref_uuid(pattern: re.Pattern[str], value: str, *, kind: str) -> UUID:
    matched = pattern.fullmatch(value)
    if matched is None:
        raise DomainConflictError(f"solution proposal has an invalid {kind} reference")
    return UUID(matched.group(1))


class SolutionProposalCommitApplication:
    """The only new-runtime path permitted to create a canonical SolutionVersion."""

    def __init__(self, session: AsyncSession, *, artifact_root: Path) -> None:
        self._session = session
        self._store = PostgresDomainStore(session)
        self._artifact_root = artifact_root

    async def commit_approved_decision(
        self,
        *,
        agent_run_decision_id: UUID,
        expected_project_revision: int,
    ) -> SolutionVersion | None:
        agent_decision = await AgentRunDecisionStore(self._session).get(agent_run_decision_id)
        if agent_decision is None:
            raise DomainNotFoundError("AgentRun decision not found")
        control = AgentRunControl(self._session)
        run = await control.get(agent_decision.agent_run_id)
        if run is None or run.kind is not AgentRunKind.SOLUTION:
            raise DomainConflictError("AgentRun decision is not a SolutionGraph decision")
        if agent_decision.status is AgentRunDecisionStatus.REJECTED:
            if run.status is AgentRunStatus.WAITING:
                await control.complete_waiting_run(run_id=run.id, succeeded=False)
                await self._session.commit()
            return None
        if agent_decision.status is not AgentRunDecisionStatus.APPROVED:
            raise DomainConflictError("Solution proposal still requires user decision")

        project = await self._store.get_project(run.project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        manifest = await self._load_ready_manifest(
            run=run, ref=agent_decision.proposal_manifest_ref
        )
        elements = await self._load_elements(run=run, manifest=manifest)
        dependencies = derive_solution_dependencies(elements)
        modules = await ProjectApplication(self._store)._active_modules(project)
        module_by_id = {item.id: item for item in modules}
        element_by_module = {item.module_id: item for item in elements.elements}
        if set(element_by_module) != set(module_by_id):
            raise DomainConflictError("solution proposal does not cover current active modules")
        if len(manifest.accepted_decision_refs) != 1:
            raise DomainConflictError("solution proposal requires exactly one accepted decision")
        decision_id = _ref_uuid(
            _DECISION_REF,
            manifest.accepted_decision_refs[0],
            kind="accepted decision",
        )
        decision = await self._store.get_decision_request(decision_id)
        if (
            decision is None
            or decision.project_id != project.id
            or decision.status is not DecisionStatus.APPROVED
            or decision.selected_option_id is None
        ):
            raise DomainConflictError("solution proposal accepted decision is unavailable")
        selected_option = next(
            (item for item in decision.options if item.option_id == decision.selected_option_id),
            None,
        )
        if selected_option is None:  # pragma: no cover - DecisionRequest guards this invariant
            raise DomainConflictError("solution proposal accepted decision is malformed")

        candidates = {item.id: item for item in await self._store.list_candidates(project.id)}
        evidence_ids: set[UUID] = set()
        selected_candidate_ids: set[UUID] = set()
        selections: list[ModuleSelection] = []
        bom: list[BomItem] = []
        implementation: list[SolutionPlanStep] = []
        verification: list[SolutionPlanStep] = []
        for module in sorted(modules, key=lambda item: item.key):
            element = element_by_module[module.id]
            candidate_id = _ref_uuid(
                _CANDIDATE_REF,
                element.selected_candidate_ref,
                kind="candidate",
            )
            candidate = candidates.get(candidate_id)
            if candidate is None or candidate.module_id != module.id:
                raise DomainConflictError("solution element candidate is outside its active module")
            selected_candidate_ids.add(candidate.id)
            element_evidence = tuple(
                _ref_uuid(_EVIDENCE_REF, ref, kind="evidence") for ref in element.evidence_refs
            )
            evidence_ids.update(element_evidence)
            selections.append(
                ModuleSelection(
                    module_id=module.id,
                    candidate_id=candidate.id,
                    candidate_name=candidate.name,
                    rationale=element.responsibility,
                    evidence_binding_ids=element_evidence,
                )
            )
            bom.append(
                BomItem(
                    line_id=f"bom-{module.id.hex}",
                    module_id=module.id,
                    candidate_id=candidate.id,
                    name=candidate.name,
                    quantity=1.0,
                    unit="item",
                    evidence_binding_ids=element_evidence,
                )
            )
            implementation.append(
                SolutionPlanStep(
                    step_id=f"implement-{module.key}",
                    title=f"Implement {module.name}",
                    instruction=(
                        f"Implement {element.responsibility} using the frozen configuration "
                        f"Artifact {element.configuration_ref}."
                    ),
                    module_ids=(module.id,),
                    acceptance=module.acceptance,
                )
            )
            verification.append(
                SolutionPlanStep(
                    step_id=f"verify-{module.key}",
                    title=f"Verify {module.name}",
                    instruction=(
                        "Verify the module against its approved acceptance criteria and the "
                        "frozen cross-module integration report."
                    ),
                    module_ids=(module.id,),
                    acceptance=module.acceptance,
                )
            )
        if selected_candidate_ids != set(selected_option.candidate_ids):
            raise DomainConflictError("solution elements do not preserve the approved decision")
        domain = ProjectApplication(self._store)
        proposal = await domain.submit_solution_proposal(
            project_id=project.id,
            expected_project_revision=expected_project_revision,
            decision_id=decision.id,
            module_selections=tuple(selections),
            evidence_binding_ids=tuple(sorted(evidence_ids, key=str)),
            compatibility_finding_ids=(),
            dependencies=dependencies,
            dependency_projection_complete=True,
            bom=tuple(bom),
            implementation_steps=tuple(implementation),
            verification_steps=tuple(verification),
            risks=(),
            unknowns=(),
            consequences=(),
            artifact_ref=agent_decision.proposal_manifest_ref,
            profile_id="langgraph-solution-composer",
            profile_revision=1,
            idempotency_key=f"solution-agent-run-proposal:{run.id}",
        )
        solution = await domain.freeze_solution(
            project_id=project.id,
            expected_project_revision=expected_project_revision + 1,
            solution_proposal_id=proposal.id,
            basis_hash=proposal.basis_hash,
            idempotency_key=f"solution-agent-run-freeze:{run.id}",
        )
        current_run = await control.get(run.id)
        if current_run is not None and current_run.status is AgentRunStatus.WAITING:
            await control.complete_waiting_run(run_id=run.id, succeeded=True)
        elif current_run is None or current_run.status is not AgentRunStatus.SUCCEEDED:
            raise DomainConflictError("Solution AgentRun is not waiting for decision completion")
        await self._session.commit()
        return solution

    async def _load_ready_manifest(
        self,
        *,
        run: AgentRun,
        ref: str,
    ) -> SolutionProposalManifest:
        payload = await ContentAddressedArtifactStore(
            self._session, self._artifact_root
        ).read_json_ref(
            project_id=run.project_id,
            basis_hash=run.basis_hash,
            ref=ref,
            expected_kind="solution_proposal_manifest",
        )
        manifest = SolutionProposalManifest.model_validate(payload)
        if (
            manifest.run_id != run.id
            or manifest.project_id != run.project_id
            or manifest.basis_hash != run.basis_hash
            or not manifest.can_create_solution_version
        ):
            raise DomainConflictError("Solution Proposal Manifest is not READY for commit")
        return manifest

    async def _load_elements(
        self,
        *,
        run: AgentRun,
        manifest: SolutionProposalManifest,
    ) -> SolutionElementSet:
        artifact_hash, _ = ContentAddressedArtifactStore.parse_ref(manifest.normalized_elements_ref)
        if artifact_hash != manifest.normalized_elements_hash:
            raise DomainConflictError("normalized solution Artifact hash is stale")
        payload = await ContentAddressedArtifactStore(
            self._session, self._artifact_root
        ).read_json_ref(
            project_id=run.project_id,
            basis_hash=run.basis_hash,
            ref=manifest.normalized_elements_ref,
            expected_kind="solution_normalized_elements",
        )
        elements = SolutionElementSet.model_validate(payload)
        return elements


__all__ = ["SolutionProposalCommitApplication"]
