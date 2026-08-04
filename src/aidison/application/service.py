from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from aidison.application.ports import DomainStore
from aidison.domain.models import (
    BomItem,
    Candidate,
    CompatibilityFinding,
    CompatibilityStatus,
    DecisionOption,
    DecisionRequest,
    DecisionStatus,
    EvidenceBinding,
    ImpactAnalysis,
    ImpactStatus,
    Module,
    ModulePatch,
    ModuleSelection,
    Observation,
    PatchSet,
    Project,
    ProjectStage,
    RequirementRevision,
    RequirementStatus,
    SolutionPlanStep,
    SolutionProposal,
    SolutionProposalStatus,
    SolutionVersion,
)


class DomainNotFoundError(RuntimeError):
    pass


class DomainConflictError(RuntimeError):
    pass


class PreconditionFailedError(DomainConflictError):
    pass


def _canonical_json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC) if value.tzinfo is not None else value
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported value in canonical payload: {type(value).__name__}")


def canonical_hash(*values: object) -> str:
    payload = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_default,
    )
    return sha256(payload.encode()).hexdigest()


class ProjectApplication:
    def __init__(self, store: DomainStore) -> None:
        self._store = store

    async def create_project(
        self,
        *,
        name: str,
        goal: str,
        idempotency_key: str,
    ) -> Project:
        payload_hash = canonical_hash("create_project", name, goal)
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            project = await self._store.get_project(UUID(receipt))
            if project is None:
                raise DomainConflictError("command receipt references a missing project")
            return project

        project = Project(name=name, goal=goal)
        await self._store.add_project(project)
        await self._store.append_event(
            project.id,
            "project.created",
            {"project_id": str(project.id), "revision": project.revision},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(project.id),
        )
        await self._store.commit()
        return project

    async def approve_requirements(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        goal: str,
        hard_constraints: Sequence[str],
        preferences: Sequence[str],
        available_resources: Sequence[str],
        unknowns: Sequence[str],
        modules: Sequence[dict[str, Any]],
        idempotency_key: str,
    ) -> tuple[RequirementRevision, tuple[Module, ...]]:
        payload_hash = canonical_hash(
            "approve_requirements",
            project_id,
            expected_project_revision,
            goal,
            hard_constraints,
            preferences,
            available_resources,
            unknowns,
            modules,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            requirement = await self._store.get_requirement_revision(UUID(receipt))
            if requirement is None:
                raise DomainConflictError("command receipt references missing requirements")
            return requirement, tuple(
                await self._store.list_modules(project_id, requirement_revision_id=requirement.id)
            )

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        previous = await self._store.list_requirement_revisions(project_id)
        requirement = RequirementRevision(
            project_id=project_id,
            revision=len(previous) + 1,
            status=RequirementStatus.APPROVED,
            goal=goal,
            hard_constraints=tuple(hard_constraints),
            preferences=tuple(preferences),
            available_resources=tuple(available_resources),
            unknowns=tuple(unknowns),
            approved_at=datetime.now(UTC),
        )
        module_drafts = tuple(
            Module(
                project_id=project_id,
                requirement_revision_id=requirement.id,
                key=item["key"],
                name=item["name"],
                responsibility=item["responsibility"],
                acceptance=tuple(item.get("acceptance", ())),
                open_questions=tuple(item.get("open_questions", ())),
            )
            for item in modules
        )
        if not 1 <= len(module_drafts) <= 8:
            raise DomainConflictError("V0 requirements must contain one to eight modules")
        modules_by_key = {item.key: item for item in module_drafts}
        if len(modules_by_key) != len(module_drafts):
            raise DomainConflictError("module keys must be unique")
        dependencies_by_key = {
            item["key"]: tuple(dict.fromkeys(item.get("dependency_keys", ()))) for item in modules
        }
        self._validate_module_graph(set(modules_by_key), dependencies_by_key)
        module_entities = tuple(
            item.model_copy(
                update={
                    "dependency_ids": tuple(
                        modules_by_key[key].id for key in dependencies_by_key[item.key]
                    )
                }
            )
            for item in module_drafts
        )

        updated_project = project.model_copy(
            update={
                "stage": ProjectStage.RESEARCH,
                "revision": project.revision + 1,
                "active_requirement_revision_id": requirement.id,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._store.update_project(
            project=updated_project,
            expected_revision=project.revision,
        )
        await self._store.add_requirement_revision(requirement)
        await self._store.add_modules(module_entities)
        await self._store.append_event(
            project_id,
            "requirements.approved",
            {"requirement_id": str(requirement.id), "module_count": len(module_entities)},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(requirement.id),
        )
        await self._store.commit()
        return requirement, module_entities

    async def submit_research_proposal(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        evidence: Sequence[EvidenceBinding],
        candidates: Sequence[Candidate],
        findings: Sequence[CompatibilityFinding],
        decision_question: str,
        decision_options: Sequence[DecisionOption],
        idempotency_key: str,
    ) -> DecisionRequest:
        payload_hash = canonical_hash(
            "submit_research_proposal",
            project_id,
            expected_project_revision,
            evidence,
            candidates,
            findings,
            decision_question,
            decision_options,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            decision = await self._store.get_decision_request(UUID(receipt))
            if decision is None:
                raise DomainConflictError("command receipt references a missing decision")
            return decision

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before research")
        for evidence_item in evidence:
            if evidence_item.project_id != project_id:
                raise DomainConflictError("proposal contains another project's data")
        for candidate_item in candidates:
            if candidate_item.project_id != project_id:
                raise DomainConflictError("proposal contains another project's data")
        for finding_item in findings:
            if finding_item.project_id != project_id:
                raise DomainConflictError("proposal contains another project's data")
        candidate_ids = {item.id for item in candidates}
        evidence_ids = {item.id for item in evidence}
        for option in decision_options:
            if option.legacy_unbound:
                raise DomainConflictError("new decisions cannot contain legacy unbound options")
            if not set(option.candidate_ids).issubset(candidate_ids):
                raise DomainConflictError("decision option references an unknown candidate")
            if not set(option.evidence_binding_ids).issubset(evidence_ids):
                raise DomainConflictError("decision option references unknown evidence")

        modules = await self._store.list_modules(
            project_id,
            requirement_revision_id=project.active_requirement_revision_id,
        )
        basis_hash = canonical_hash(
            project.active_requirement_revision_id,
            modules,
            evidence,
            candidates,
            findings,
        )
        decision = DecisionRequest(
            project_id=project_id,
            basis_hash=basis_hash,
            question=decision_question,
            options=tuple(decision_options),
            affected_module_ids=tuple(module.id for module in modules),
        )
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "stage": ProjectStage.DECIDING,
                    "revision": project.revision + 1,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.add_evidence_bindings(evidence)
        await self._store.add_candidates(candidates)
        await self._store.add_compatibility_findings(findings)
        await self._store.add_decision_request(decision)
        await self._store.append_event(
            project_id,
            "decision.required",
            {"decision_id": str(decision.id), "basis_hash": basis_hash},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(decision.id),
        )
        await self._store.commit()
        return decision

    async def resolve_decision(
        self,
        *,
        decision_id: UUID,
        expected_project_revision: int,
        selected_option_id: str,
        basis_hash: str,
        idempotency_key: str,
    ) -> DecisionRequest:
        payload_hash = canonical_hash(
            "resolve_decision",
            decision_id,
            expected_project_revision,
            selected_option_id,
            basis_hash,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            decision = await self._store.get_decision_request(UUID(receipt))
            if decision is None:
                raise DomainConflictError("command receipt references a missing decision")
            return decision

        decision = await self._store.get_decision_request(decision_id)
        if decision is None:
            raise DomainNotFoundError("decision not found")
        if decision.status is not DecisionStatus.PENDING:
            raise DomainConflictError("decision is already resolved")
        if decision.basis_hash != basis_hash:
            raise PreconditionFailedError("decision basis is stale")
        project = await self._required_project(decision.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        resolved = DecisionRequest.model_validate(
            decision.model_copy(
                update={
                    "status": DecisionStatus.APPROVED,
                    "selected_option_id": selected_option_id,
                    "resolved_at": datetime.now(UTC),
                }
            ).model_dump()
        )
        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
            ),
            expected_revision=project.revision,
        )
        await self._store.update_decision_request(resolved)
        await self._store.append_event(
            decision.project_id,
            "decision.resolved",
            {"decision_id": str(decision.id), "selected_option_id": selected_option_id},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(decision.id),
        )
        await self._store.commit()
        return resolved

    async def freeze_solution(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        solution_proposal_id: UUID,
        basis_hash: str,
        idempotency_key: str,
    ) -> SolutionVersion:
        payload_hash = canonical_hash(
            "freeze_solution",
            project_id,
            expected_project_revision,
            solution_proposal_id,
            basis_hash,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            solution = await self._store.get_solution_version(UUID(receipt))
            if solution is None:
                raise DomainConflictError("command receipt references a missing solution")
            return solution

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        proposal = await self._store.get_solution_proposal(solution_proposal_id)
        if proposal is None or proposal.project_id != project_id:
            raise DomainNotFoundError("solution proposal not found")
        if proposal.status is not SolutionProposalStatus.PROPOSED:
            raise DomainConflictError("solution proposal is already resolved")
        if proposal.basis_hash != basis_hash:
            raise PreconditionFailedError("solution proposal basis is stale")
        decision = await self._store.get_decision_request(proposal.decision_id)
        if decision is None or decision.project_id != project_id:
            raise DomainNotFoundError("decision not found")
        if decision.status is not DecisionStatus.APPROVED:
            raise DomainConflictError("solution requires an approved decision")
        findings = {
            item.id: item for item in await self._store.list_compatibility_findings(project_id)
        }
        if any(
            findings[item_id].status is CompatibilityStatus.INCOMPATIBLE
            for item_id in proposal.compatibility_finding_ids
        ):
            raise DomainConflictError("incompatible finding blocks solution freeze")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("project has no approved requirements")
        if proposal.requirement_revision_id != project.active_requirement_revision_id:
            raise PreconditionFailedError("solution proposal requirements are stale")
        existing = await self._store.list_solution_versions(project_id)
        solution = SolutionVersion(
            project_id=project_id,
            version=len(existing) + 1,
            requirement_revision_id=project.active_requirement_revision_id,
            basis_hash=proposal.basis_hash,
            module_snapshots=tuple(
                {
                    **item.model_dump(mode="json"),
                    "snapshot_hash": canonical_hash(item),
                }
                for item in proposal.module_selections
            ),
            evidence_binding_ids=proposal.evidence_binding_ids,
            compatibility_finding_ids=proposal.compatibility_finding_ids,
            bom=tuple(item.model_dump(mode="json") for item in proposal.bom),
            implementation_steps=tuple(
                item.model_dump(mode="json") for item in proposal.implementation_steps
            ),
            verification_steps=tuple(
                item.model_dump(mode="json") for item in proposal.verification_steps
            ),
            approved_decision_id=decision.id,
            solution_proposal_id=proposal.id,
        )
        approved_proposal = proposal.model_copy(
            update={
                "status": SolutionProposalStatus.APPROVED,
                "resolved_at": datetime.now(UTC),
            }
        )
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "stage": ProjectStage.APPROVED,
                    "revision": project.revision + 1,
                    "active_solution_version_id": solution.id,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.add_solution_version(solution)
        await self._store.update_solution_proposal(approved_proposal)
        await self._store.append_event(
            project_id,
            "solution.frozen",
            {"solution_id": str(solution.id), "version": solution.version},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(solution.id),
        )
        await self._store.commit()
        return solution

    async def submit_solution_proposal(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        decision_id: UUID,
        module_selections: Sequence[ModuleSelection],
        evidence_binding_ids: Sequence[UUID],
        compatibility_finding_ids: Sequence[UUID],
        bom: Sequence[BomItem],
        implementation_steps: Sequence[SolutionPlanStep],
        verification_steps: Sequence[SolutionPlanStep],
        risks: Sequence[str],
        unknowns: Sequence[str],
        consequences: Sequence[str],
        artifact_ref: str,
        profile_id: str,
        profile_revision: int,
        idempotency_key: str,
    ) -> SolutionProposal:
        proposal_basis = canonical_hash(
            decision_id,
            module_selections,
            evidence_binding_ids,
            compatibility_finding_ids,
            bom,
            implementation_steps,
            verification_steps,
            risks,
            unknowns,
            consequences,
            artifact_ref,
            profile_id,
            profile_revision,
        )
        payload_hash = canonical_hash(
            "submit_solution_proposal",
            project_id,
            expected_project_revision,
            proposal_basis,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            proposal = await self._store.get_solution_proposal(UUID(receipt))
            if proposal is None:
                raise DomainConflictError("command receipt references a missing solution proposal")
            return proposal

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("solution proposal requires approved requirements")
        decision = await self._store.get_decision_request(decision_id)
        if decision is None or decision.project_id != project_id:
            raise DomainNotFoundError("decision not found")
        if decision.status is not DecisionStatus.APPROVED:
            raise DomainConflictError("solution proposal requires an approved decision")

        modules = tuple(
            await self._store.list_modules(
                project_id,
                requirement_revision_id=project.active_requirement_revision_id,
            )
        )
        module_ids = {item.id for item in modules}
        selections = tuple(module_selections)
        if {item.module_id for item in selections} != module_ids or len(selections) != len(
            module_ids
        ):
            raise DomainConflictError("solution proposal must select every active module once")

        candidates = {item.id: item for item in await self._store.list_candidates(project_id)}
        evidence = {item.id: item for item in await self._store.list_evidence_bindings(project_id)}
        findings = {
            item.id: item for item in await self._store.list_compatibility_findings(project_id)
        }
        proposal_evidence_ids = tuple(dict.fromkeys(evidence_binding_ids))
        proposal_finding_ids = tuple(dict.fromkeys(compatibility_finding_ids))
        if not set(proposal_evidence_ids) <= set(evidence):
            raise DomainConflictError("solution proposal references unknown evidence")
        if not set(proposal_finding_ids) <= set(findings):
            raise DomainConflictError("solution proposal references unknown compatibility findings")
        for selection in selections:
            candidate = candidates.get(selection.candidate_id)
            if (
                candidate is None
                or candidate.module_id != selection.module_id
                or candidate.name != selection.candidate_name
            ):
                raise DomainConflictError("solution selection does not match a project candidate")
            if not set(selection.evidence_binding_ids) <= set(candidate.evidence_binding_ids):
                raise DomainConflictError("solution selection has unbound evidence")

        selected_pairs = {(item.module_id, item.candidate_id) for item in selections}
        line_ids: set[str] = set()
        for item in bom:
            if item.line_id in line_ids:
                raise DomainConflictError("BOM line IDs must be unique")
            line_ids.add(item.line_id)
            if (item.module_id, item.candidate_id) not in selected_pairs:
                raise DomainConflictError("BOM item is not bound to a selected candidate")
            if not set(item.evidence_binding_ids) <= set(proposal_evidence_ids):
                raise DomainConflictError("BOM item references evidence outside the proposal")

        implementation = tuple(implementation_steps)
        verification = tuple(verification_steps)
        for step in (*implementation, *verification):
            if not set(step.module_ids) <= module_ids:
                raise DomainConflictError("solution step references a module outside the project")
        verification_scope = {module_id for step in verification for module_id in step.module_ids}
        needs_verification = {
            module_id
            for finding_id in proposal_finding_ids
            if findings[finding_id].status
            in {CompatibilityStatus.UNKNOWN, CompatibilityStatus.NEEDS_TEST}
            for module_id in findings[finding_id].module_ids
        }
        if not needs_verification <= verification_scope:
            raise DomainConflictError("unknown or needs_test findings require verification steps")

        proposal = SolutionProposal(
            project_id=project_id,
            decision_id=decision.id,
            requirement_revision_id=project.active_requirement_revision_id,
            basis_hash=proposal_basis,
            module_selections=selections,
            evidence_binding_ids=proposal_evidence_ids,
            compatibility_finding_ids=proposal_finding_ids,
            bom=tuple(bom),
            implementation_steps=implementation,
            verification_steps=verification,
            risks=tuple(risks),
            unknowns=tuple(unknowns),
            consequences=tuple(consequences),
            artifact_ref=artifact_ref,
            profile_id=profile_id,
            profile_revision=profile_revision,
        )
        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
            ),
            expected_revision=project.revision,
        )
        await self._store.add_solution_proposal(proposal)
        await self._store.append_event(
            project_id,
            "solution.proposed",
            {"solution_proposal_id": str(proposal.id), "basis_hash": proposal.basis_hash},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(proposal.id),
        )
        await self._store.commit()
        return proposal

    async def submit_observation(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        statement: str,
        affected_module_ids: Sequence[UUID],
        idempotency_key: str,
    ) -> Observation:
        payload_hash = canonical_hash(
            "submit_observation",
            project_id,
            expected_project_revision,
            statement,
            affected_module_ids,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            observation = await self._store.get_observation(UUID(receipt))
            if observation is None:
                raise DomainConflictError("command receipt references a missing observation")
            return observation

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_solution_version_id is None:
            raise DomainConflictError("observation requires an active solution")
        solution = await self._store.get_solution_version(project.active_solution_version_id)
        if solution is None:
            raise DomainConflictError("active solution is missing")
        modules = await self._store.list_modules(
            project_id,
            requirement_revision_id=project.active_requirement_revision_id,
        )
        module_ids = {module.id for module in modules}
        direct_affected = tuple(dict.fromkeys(affected_module_ids))
        if not direct_affected or not set(direct_affected) <= module_ids:
            raise DomainConflictError("observation must reference project modules")
        observation = Observation(
            project_id=project_id,
            solution_version_id=solution.id,
            statement=statement,
            affected_module_hints=direct_affected,
        )
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "stage": ProjectStage.REVISING,
                    "revision": project.revision + 1,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.add_observation(observation)
        await self._store.append_event(
            project_id,
            "observation.recorded",
            {
                "observation_id": str(observation.id),
                "direct_affected_count": len(direct_affected),
            },
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(observation.id),
        )
        await self._store.commit()
        return observation

    async def submit_impact_analysis(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        observation_id: UUID,
        module_patches: Sequence[ModulePatch],
        stale_evidence_binding_ids: Sequence[UUID],
        replacement_bom_items: Sequence[BomItem],
        replacement_implementation_steps: Sequence[SolutionPlanStep],
        replacement_verification_steps: Sequence[SolutionPlanStep],
        summary: str,
        risks: Sequence[str],
        artifact_ref: str,
        profile_id: str,
        profile_revision: int,
        idempotency_key: str,
    ) -> ImpactAnalysis:
        proposal_basis = canonical_hash(
            observation_id,
            module_patches,
            stale_evidence_binding_ids,
            replacement_bom_items,
            replacement_implementation_steps,
            replacement_verification_steps,
            summary,
            risks,
            artifact_ref,
            profile_id,
            profile_revision,
        )
        payload_hash = canonical_hash(
            "submit_impact_analysis",
            project_id,
            expected_project_revision,
            proposal_basis,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            impact = await self._store.get_impact_analysis(UUID(receipt))
            if impact is None:
                raise DomainConflictError("command receipt references a missing impact")
            return impact

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        observation = await self._store.get_observation(observation_id)
        if observation is None or observation.project_id != project_id:
            raise DomainNotFoundError("observation not found")
        if project.active_solution_version_id != observation.solution_version_id:
            raise PreconditionFailedError("observation base is no longer the active solution")
        base = await self._store.get_solution_version(observation.solution_version_id)
        if base is None:
            raise DomainConflictError("observation base solution is missing")
        modules = tuple(
            await self._store.list_modules(
                project_id,
                requirement_revision_id=project.active_requirement_revision_id,
            )
        )
        direct, transitive, affected, unaffected = self._impact_partition(
            modules,
            observation.affected_module_hints,
        )
        affected_set = set(affected)
        patches = tuple(module_patches)
        if not patches or len({item.module_id for item in patches}) != len(patches):
            raise DomainConflictError("impact proposal requires unique module patches")
        if not {item.module_id for item in patches} <= affected_set:
            raise DomainConflictError("impact proposal cannot patch unaffected modules")

        snapshots = {UUID(str(item["module_id"])): item for item in base.module_snapshots}
        candidates = {item.id: item for item in await self._store.list_candidates(project_id)}
        evidence = {item.id: item for item in await self._store.list_evidence_bindings(project_id)}
        for patch in patches:
            snapshot = snapshots.get(patch.module_id)
            candidate = candidates.get(patch.replacement.candidate_id)
            if snapshot is None or snapshot.get("snapshot_hash") != patch.base_snapshot_hash:
                raise PreconditionFailedError("impact patch base snapshot is stale")
            if (
                candidate is None
                or candidate.module_id != patch.module_id
                or candidate.name != patch.replacement.candidate_name
            ):
                raise DomainConflictError("impact patch does not match a project candidate")
            if not set(patch.replacement.evidence_binding_ids) <= set(
                candidate.evidence_binding_ids
            ):
                raise DomainConflictError("impact patch has unbound evidence")

        stale_ids = tuple(dict.fromkeys(stale_evidence_binding_ids))
        if not set(stale_ids) <= set(evidence) or any(
            evidence[item_id].module_id not in affected_set for item_id in stale_ids
        ):
            raise DomainConflictError("stale evidence must belong to affected project modules")
        replacement_pairs = {(item.module_id, item.replacement.candidate_id) for item in patches}
        replacement_bom = tuple(replacement_bom_items)
        if any(
            item.module_id not in affected_set
            or (item.module_id, item.candidate_id) not in replacement_pairs
            or not set(item.evidence_binding_ids)
            <= set(candidates[item.candidate_id].evidence_binding_ids)
            for item in replacement_bom
        ):
            raise DomainConflictError("replacement BOM must bind affected patch candidates")
        implementation = tuple(replacement_implementation_steps)
        verification = tuple(replacement_verification_steps)
        if (
            not implementation
            or not verification
            or any(
                not set(step.module_ids) or not set(step.module_ids) <= affected_set
                for step in (*implementation, *verification)
            )
        ):
            raise DomainConflictError("replacement steps must stay inside affected modules")

        impact = ImpactAnalysis(
            project_id=project_id,
            observation_id=observation.id,
            base_solution_version_id=base.id,
            basis_hash=proposal_basis,
            direct_affected_module_ids=direct,
            transitive_affected_module_ids=transitive,
            affected_module_ids=affected,
            unaffected_module_ids=unaffected,
            stale_evidence_binding_ids=stale_ids,
            module_patches=patches,
            replacement_bom_items=replacement_bom,
            replacement_implementation_steps=implementation,
            replacement_verification_steps=verification,
            summary=summary,
            risks=tuple(risks),
            artifact_ref=artifact_ref,
            profile_id=profile_id,
            profile_revision=profile_revision,
        )
        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
            ),
            expected_revision=project.revision,
        )
        await self._store.add_impact_analysis(impact)
        await self._store.append_event(
            project_id,
            "impact.proposed",
            {
                "impact_id": str(impact.id),
                "direct_affected_count": len(direct),
                "transitive_affected_count": len(transitive),
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(impact.id))
        await self._store.commit()
        return impact

    async def approve_impact_and_patch(
        self,
        *,
        impact_id: UUID,
        expected_project_revision: int,
        basis_hash: str,
        idempotency_key: str,
    ) -> tuple[PatchSet, SolutionVersion]:
        payload_hash = canonical_hash(
            "approve_impact_and_patch",
            impact_id,
            expected_project_revision,
            basis_hash,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            patch_set = await self._store.get_patch_set(UUID(receipt))
            if patch_set is None:
                raise DomainConflictError("command receipt references a missing patch set")
            solution_id = self._revision_solution_id(patch_set.id)
            solution = await self._store.get_solution_version(solution_id)
            if solution is None:
                raise DomainConflictError("command receipt references a missing solution")
            return patch_set, solution

        impact = await self._store.get_impact_analysis(impact_id)
        if impact is None:
            raise DomainNotFoundError("impact analysis not found")
        if impact.status is not ImpactStatus.PROPOSED:
            raise DomainConflictError("impact analysis is already resolved")
        if impact.basis_hash != basis_hash:
            raise PreconditionFailedError("impact proposal basis is stale")
        if impact.artifact_ref is None or impact.profile_id is None or not impact.module_patches:
            raise DomainConflictError("legacy impact analysis cannot be strictly approved")
        base = await self._store.get_solution_version(impact.base_solution_version_id)
        if base is None:
            raise PreconditionFailedError("impact base is missing")
        project = await self._required_project(impact.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_solution_version_id != base.id:
            raise PreconditionFailedError("impact base is no longer the active solution")
        patch_set = PatchSet(
            project_id=impact.project_id,
            impact_analysis_id=impact.id,
            base_solution_version_id=base.id,
            base_solution_basis_hash=base.basis_hash,
            typed_module_patches=impact.module_patches,
            replacement_bom_items=impact.replacement_bom_items,
            replacement_implementation_steps=impact.replacement_implementation_steps,
            replacement_verification_steps=impact.replacement_verification_steps,
        )
        affected_set = set(impact.affected_module_ids)
        if not {item.module_id for item in patch_set.typed_module_patches} <= affected_set:
            raise DomainConflictError("patch set cannot touch unaffected modules")
        replacements = {item.module_id: item for item in patch_set.typed_module_patches}
        patched_set = set(replacements)
        snapshots = tuple(
            (
                {
                    **replacements[UUID(str(snapshot["module_id"]))].replacement.model_dump(
                        mode="json"
                    ),
                    "snapshot_hash": canonical_hash(
                        replacements[UUID(str(snapshot["module_id"]))].replacement
                    ),
                }
                if UUID(str(snapshot["module_id"])) in replacements
                else snapshot
            )
            for snapshot in base.module_snapshots
        )
        bom = tuple(
            item for item in base.bom if UUID(str(item["module_id"])) not in patched_set
        ) + tuple(item.model_dump(mode="json") for item in patch_set.replacement_bom_items)

        def merge_steps(
            base_steps: Sequence[dict[str, Any]],
            replacements: Sequence[SolutionPlanStep],
        ) -> tuple[dict[str, Any], ...]:
            replacement_scope = {
                module_id for step in replacements for module_id in step.module_ids
            }
            preserved = tuple(
                item
                for item in base_steps
                if set(UUID(str(value)) for value in item.get("module_ids", ())).isdisjoint(
                    replacement_scope
                )
            )
            return preserved + tuple(item.model_dump(mode="json") for item in replacements)

        implementation = merge_steps(
            base.implementation_steps,
            patch_set.replacement_implementation_steps,
        )
        verification = merge_steps(
            base.verification_steps,
            patch_set.replacement_verification_steps,
        )
        stale_evidence = set(impact.stale_evidence_binding_ids)
        replacement_evidence = {
            evidence_id
            for patch in patch_set.typed_module_patches
            for evidence_id in patch.replacement.evidence_binding_ids
        }
        next_solution = SolutionVersion.model_validate(
            base.model_copy(
                update={
                    "id": self._revision_solution_id(patch_set.id),
                    "version": base.version + 1,
                    "basis_hash": canonical_hash(base.basis_hash, patch_set),
                    "module_snapshots": snapshots,
                    "evidence_binding_ids": tuple(
                        dict.fromkeys(
                            (
                                *(
                                    item
                                    for item in base.evidence_binding_ids
                                    if item not in stale_evidence
                                ),
                                *replacement_evidence,
                            )
                        )
                    ),
                    "bom": bom,
                    "implementation_steps": implementation,
                    "verification_steps": verification,
                    "previous_version_id": base.id,
                    "created_at": datetime.now(UTC),
                }
            ).model_dump()
        )
        approved_impact = impact.model_copy(
            update={"status": ImpactStatus.APPROVED, "resolved_at": datetime.now(UTC)}
        )
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "stage": ProjectStage.APPROVED,
                    "revision": project.revision + 1,
                    "active_solution_version_id": next_solution.id,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.add_patch_set(patch_set)
        await self._store.update_impact_analysis(approved_impact)
        await self._store.add_solution_version(next_solution)
        await self._store.append_event(
            impact.project_id,
            "solution.revised",
            {
                "solution_id": str(next_solution.id),
                "previous_solution_id": str(base.id),
                "version": next_solution.version,
                "affected_module_ids": [str(item) for item in impact.affected_module_ids],
                "reused_module_ids": [str(item) for item in impact.unaffected_module_ids],
            },
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(patch_set.id),
        )
        await self._store.commit()
        return patch_set, next_solution

    @staticmethod
    def _impact_partition(
        modules: Sequence[Module],
        direct_module_ids: Sequence[UUID],
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...]]:
        module_ids = {item.id for item in modules}
        direct_set = set(direct_module_ids)
        if not direct_set or not direct_set <= module_ids:
            raise DomainConflictError("impact must reference active project modules")
        affected_set = set(direct_set)
        changed = True
        while changed:
            changed = False
            for module in modules:
                if module.id not in affected_set and affected_set.intersection(
                    module.dependency_ids
                ):
                    affected_set.add(module.id)
                    changed = True
        ordered = tuple(sorted(modules, key=lambda item: item.key))
        direct = tuple(item.id for item in ordered if item.id in direct_set)
        transitive = tuple(
            item.id for item in ordered if item.id in affected_set and item.id not in direct_set
        )
        affected = tuple(item.id for item in ordered if item.id in affected_set)
        unaffected = tuple(item.id for item in ordered if item.id not in affected_set)
        return direct, transitive, affected, unaffected

    @staticmethod
    def _revision_solution_id(patch_set_id: UUID) -> UUID:
        return UUID(bytes=sha256(str(patch_set_id).encode()).digest()[:16])

    async def _required_project(self, project_id: UUID) -> Project:
        project = await self._store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        return project

    @staticmethod
    def _validate_module_graph(
        module_keys: set[str],
        dependencies_by_key: dict[str, tuple[str, ...]],
    ) -> None:
        for key, dependencies in dependencies_by_key.items():
            unknown = set(dependencies) - module_keys
            if unknown:
                raise DomainConflictError(
                    f"module {key} has unknown dependencies: {sorted(unknown)}"
                )
            if key in dependencies:
                raise DomainConflictError(f"module {key} cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise DomainConflictError("module dependencies contain a cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in dependencies_by_key[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in sorted(module_keys):
            visit(key)
