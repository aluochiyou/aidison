"""Translate an approved single-task ProposalManifest into canonical Project facts."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aidison.application.service import (
    DomainConflictError,
    DomainNotFoundError,
    PreconditionFailedError,
    ProjectApplication,
)
from aidison.application.single_task_research import SingleTaskResearchPayload
from aidison.domain.models import (
    Candidate,
    DecisionOption,
    DecisionRequest,
    EvidenceBinding,
    Module,
)
from aidison.infrastructure.agent_decisions import AgentRunDecisionStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.decision_contracts import AgentRunDecisionStatus
from aidison.research.evidence_admission import (
    AdmittedEvidenceBinding,
    to_legacy_domain_evidence_binding,
)


class ProposalCommitApplication:
    """Only this application boundary may write canonical project facts from a proposal."""

    def __init__(self, session: AsyncSession, *, artifact_root: Path) -> None:
        self._session = session
        self._store = PostgresDomainStore(session)
        self._artifact_root = artifact_root

    async def commit_approved_decision(
        self,
        *,
        agent_run_decision_id: UUID,
        expected_project_revision: int,
    ) -> DecisionRequest | None:
        agent_decision = await AgentRunDecisionStore(self._session).get(agent_run_decision_id)
        if agent_decision is None:
            raise DomainNotFoundError("AgentRun decision not found")
        if agent_decision.status is AgentRunDecisionStatus.REJECTED:
            await AgentRunControl(self._session).complete_waiting_run(
                run_id=agent_decision.agent_run_id,
                succeeded=False,
            )
            await self._session.commit()
            return None
        if agent_decision.status is not AgentRunDecisionStatus.APPROVED:
            raise DomainConflictError("AgentRun proposal still requires user decision")

        project = await self._store.get_project(agent_decision.project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        modules = await ProjectApplication(self._store)._active_modules(project)
        if not modules:
            raise DomainConflictError("approved proposal requires an active module")
        manifest = await ContentAddressedArtifactStore(
            self._session, self._artifact_root
        ).read_json_ref(
            project_id=project.id,
            basis_hash=agent_decision.basis_hash,
            ref=agent_decision.proposal_manifest_ref,
            expected_kind="research_proposal_manifest",
        )
        if not isinstance(manifest, dict):
            raise DomainConflictError("proposal manifest must be an object")
        if manifest.get("schema_version") == "research-proposal-manifest-v2":
            question, evidence, candidates, options = await _multi_task_canonical_projection(
                project_id=project.id,
                basis_hash=agent_decision.basis_hash,
                modules=modules,
                manifest=manifest,
                artifacts=ContentAddressedArtifactStore(self._session, self._artifact_root),
            )
        elif manifest.get("schema_version") == "research-proposal-manifest-v3":
            question, evidence, candidates, options = await _adaptive_task_canonical_projection(
                project_id=project.id,
                basis_hash=agent_decision.basis_hash,
                modules=modules,
                manifest=manifest,
                artifacts=ContentAddressedArtifactStore(self._session, self._artifact_root),
            )
        else:
            payload = SingleTaskResearchPayload.model_validate(manifest.get("payload"))
            module = modules[0]
            evidence = await _read_admitted_evidence_bindings(
                artifacts=ContentAddressedArtifactStore(self._session, self._artifact_root),
                project_id=project.id,
                basis_hash=agent_decision.basis_hash,
                evidence_refs=_manifest_evidence_refs(manifest),
            )
            if not evidence or any(item.module_id != module.id for item in evidence):
                raise DomainConflictError(
                    "single-task proposal lacks active-module admitted evidence"
                )
            labels = (payload.recommended_option, *payload.alternatives)
            candidates = tuple(
                Candidate(
                    project_id=project.id,
                    module_id=module.id,
                    name=label,
                    description=payload.summary,
                    evidence_binding_ids=tuple(item.id for item in evidence),
                )
                for label in labels
            )
            options = tuple(
                DecisionOption(
                    option_id=f"option-{index + 1}",
                    label=candidate.name,
                    summary=payload.summary,
                    candidate_ids=(candidate.id,),
                    evidence_binding_ids=tuple(item.id for item in evidence),
                )
                for index, candidate in enumerate(candidates)
            )
            question = payload.question
        if len(options) < 2:
            raise DomainConflictError(
                "single-task proposal requires a recommended option and alternative"
            )
        domain = ProjectApplication(self._store)
        decision = await domain.submit_research_proposal(
            project_id=project.id,
            expected_project_revision=expected_project_revision,
            evidence=evidence,
            candidates=candidates,
            findings=(),
            decision_question=question,
            decision_options=options,
            idempotency_key=f"agent-run-proposal:{agent_decision.agent_run_id}",
        )
        selected = await domain.resolve_decision(
            decision_id=decision.id,
            expected_project_revision=expected_project_revision + 1,
            selected_option_id=options[0].option_id,
            basis_hash=decision.basis_hash,
            idempotency_key=f"agent-run-proposal-selection:{agent_decision.agent_run_id}",
        )
        await AgentRunControl(self._session).complete_waiting_run(
            run_id=agent_decision.agent_run_id,
            succeeded=True,
        )
        await self._session.commit()
        return selected


async def _multi_task_canonical_projection(
    *,
    project_id: UUID,
    basis_hash: str,
    modules: tuple[Module, ...],
    manifest: dict[object, object],
    artifacts: ContentAddressedArtifactStore,
) -> tuple[str, tuple[EvidenceBinding, ...], tuple[Candidate, ...], tuple[DecisionOption, ...]]:
    """Map deterministic per-module research outputs to one integrated choice.

    The reducer does not rank facts with another model.  It preserves the
    stable task/module order, preserves only already-admitted evidence per module, and
    offers a recommended combination plus one bounded alternative combination.
    """

    question = manifest.get("question")
    rows = manifest.get("task_results")
    if not isinstance(question, str) or not question.strip() or not isinstance(rows, list):
        raise DomainConflictError("multi-task proposal manifest is malformed")
    module_by_id = {module.id: module for module in modules}
    parsed: list[tuple[Module, SingleTaskResearchPayload, tuple[str, ...]]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise DomainConflictError("multi-task proposal has an invalid task result")
        module_text = row.get("module_id")
        task_manifest_ref = row.get("task_manifest_ref")
        task_manifest_hash = row.get("task_manifest_hash")
        if (
            not isinstance(module_text, str)
            or not isinstance(task_manifest_ref, str)
            or not isinstance(task_manifest_hash, str)
        ):
            raise DomainConflictError("multi-task proposal task references are invalid")
        try:
            module_id = UUID(module_text)
        except ValueError as error:
            raise DomainConflictError("multi-task proposal module identifier is invalid") from error
        if module_id not in module_by_id:
            raise DomainConflictError("multi-task proposal references an inactive module")
        evidence_refs = _row_evidence_refs(row)
        parsed.append(
            (
                module_by_id[module_id],
                SingleTaskResearchPayload.model_validate(row.get("payload")),
                evidence_refs,
            )
        )
    if len(parsed) != len(module_by_id) or {item[0].id for item in parsed} != set(module_by_id):
        raise DomainConflictError(
            "multi-task proposal does not cover each active module exactly once"
        )

    evidence: list[EvidenceBinding] = []
    candidates: list[Candidate] = []
    recommended_ids: list[UUID] = []
    alternative_ids: list[UUID] = []
    for module, payload, evidence_refs in sorted(parsed, key=lambda item: item[0].key):
        module_evidence = await _read_admitted_evidence_bindings(
            artifacts=artifacts,
            project_id=project_id,
            basis_hash=basis_hash,
            evidence_refs=evidence_refs,
        )
        if not module_evidence or any(item.module_id != module.id for item in module_evidence):
            raise DomainConflictError("multi-task proposal lacks active-module admitted evidence")
        evidence.extend(module_evidence)
        evidence_ids = tuple(item.id for item in module_evidence)
        recommended = Candidate(
            project_id=project_id,
            module_id=module.id,
            name=payload.recommended_option,
            description=payload.summary,
            evidence_binding_ids=evidence_ids,
        )
        alternative = Candidate(
            project_id=project_id,
            module_id=module.id,
            name=payload.alternatives[0],
            description=payload.summary,
            evidence_binding_ids=evidence_ids,
        )
        candidates.extend((recommended, alternative))
        recommended_ids.append(recommended.id)
        alternative_ids.append(alternative.id)
    evidence_ids = tuple(item.id for item in evidence)
    return (
        question,
        tuple(evidence),
        tuple(candidates),
        (
            DecisionOption(
                option_id="option-1",
                label="Recommended integrated solution",
                summary="Per-module recommended research choices.",
                candidate_ids=tuple(recommended_ids),
                evidence_binding_ids=evidence_ids,
            ),
            DecisionOption(
                option_id="option-2",
                label="Alternative integrated solution",
                summary="Per-module bounded alternative choices.",
                candidate_ids=tuple(alternative_ids),
                evidence_binding_ids=evidence_ids,
            ),
        ),
    )


async def _adaptive_task_canonical_projection(
    *,
    project_id: UUID,
    basis_hash: str,
    modules: tuple[Module, ...],
    manifest: dict[object, object],
    artifacts: ContentAddressedArtifactStore,
) -> tuple[str, tuple[EvidenceBinding, ...], tuple[Candidate, ...], tuple[DecisionOption, ...]]:
    """Reduce initial and bounded-gap task results to one option per module.

    The latest admitted task payload for a module supplies the user-facing
    wording; every admitted evidence reference accumulated by its earlier and
    later tasks remains bound to both candidates. This keeps a gap patch from
    silently disappearing at the canonical-write boundary.
    """

    question = manifest.get("question")
    rows = manifest.get("task_results")
    if not isinstance(question, str) or not question.strip() or not isinstance(rows, list):
        raise DomainConflictError("adaptive proposal manifest is malformed")
    module_by_id = {module.id: module for module in modules}
    grouped: dict[
        UUID, list[tuple[int, str, str, str, SingleTaskResearchPayload, tuple[str, ...]]]
    ] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DomainConflictError("adaptive proposal has an invalid task result")
        module_text = row.get("module_id")
        task_key = row.get("task_key")
        task_id = row.get("task_id")
        capability = row.get("capability")
        plan_revision = row.get("plan_revision")
        task_manifest_ref = row.get("task_manifest_ref")
        task_manifest_hash = row.get("task_manifest_hash")
        if (
            not isinstance(module_text, str)
            or not isinstance(task_key, str)
            or not isinstance(task_id, str)
            or capability not in {"research", "research_verifier"}
            or not isinstance(plan_revision, int)
            or not isinstance(task_manifest_ref, str)
            or not isinstance(task_manifest_hash, str)
        ):
            raise DomainConflictError("adaptive proposal task references are invalid")
        try:
            module_id = UUID(module_text)
        except ValueError as error:
            raise DomainConflictError("adaptive proposal module identifier is invalid") from error
        if module_id not in module_by_id:
            raise DomainConflictError("adaptive proposal references an inactive module")
        grouped.setdefault(module_id, []).append(
            (
                plan_revision,
                task_key,
                task_id,
                capability,
                SingleTaskResearchPayload.model_validate(row.get("payload")),
                _optional_row_evidence_refs(row),
            )
        )
    if set(grouped) != set(module_by_id):
        raise DomainConflictError("adaptive proposal does not cover each active module")

    evidence: list[EvidenceBinding] = []
    candidates: list[Candidate] = []
    recommended_ids: list[UUID] = []
    alternative_ids: list[UUID] = []
    for module_id, module in sorted(module_by_id.items(), key=lambda item: item[1].key):
        entries = sorted(grouped[module_id], key=lambda item: (item[0], item[1], item[2]))
        primary_entries = tuple(item for item in entries if item[3] == "research")
        if not primary_entries:
            raise DomainConflictError("adaptive proposal lacks a primary research task")
        payload = primary_entries[-1][4]
        evidence_refs = tuple(sorted({ref for entry in entries for ref in entry[5]}))
        module_evidence = await _read_admitted_evidence_bindings(
            artifacts=artifacts,
            project_id=project_id,
            basis_hash=basis_hash,
            evidence_refs=evidence_refs,
        )
        if not module_evidence or any(item.module_id != module.id for item in module_evidence):
            raise DomainConflictError("adaptive proposal lacks active-module admitted evidence")
        evidence.extend(module_evidence)
        evidence_ids = tuple(item.id for item in module_evidence)
        recommended = Candidate(
            project_id=project_id,
            module_id=module.id,
            name=payload.recommended_option,
            description=payload.summary,
            evidence_binding_ids=evidence_ids,
        )
        alternative = Candidate(
            project_id=project_id,
            module_id=module.id,
            name=payload.alternatives[0],
            description=payload.summary,
            evidence_binding_ids=evidence_ids,
        )
        candidates.extend((recommended, alternative))
        recommended_ids.append(recommended.id)
        alternative_ids.append(alternative.id)
    evidence_ids = tuple(item.id for item in evidence)
    return (
        question,
        tuple(evidence),
        tuple(candidates),
        (
            DecisionOption(
                option_id="option-1",
                label="Recommended integrated solution",
                summary="Per-module research choices after bounded evidence completion.",
                candidate_ids=tuple(recommended_ids),
                evidence_binding_ids=evidence_ids,
            ),
            DecisionOption(
                option_id="option-2",
                label="Alternative integrated solution",
                summary="Per-module bounded alternative choices after evidence completion.",
                candidate_ids=tuple(alternative_ids),
                evidence_binding_ids=evidence_ids,
            ),
        ),
    )


async def _read_admitted_evidence_bindings(
    *,
    artifacts: ContentAddressedArtifactStore,
    project_id: UUID,
    basis_hash: str,
    evidence_refs: tuple[str, ...],
) -> tuple[EvidenceBinding, ...]:
    if not evidence_refs:
        return ()
    bindings: dict[UUID, EvidenceBinding] = {}
    for ref in evidence_refs:
        value = await artifacts.read_json_ref(
            project_id=project_id,
            basis_hash=basis_hash,
            ref=ref,
            expected_kind="research_admitted_evidence",
        )
        modern = AdmittedEvidenceBinding.model_validate(value)
        if modern.project_id != project_id or modern.basis_hash != basis_hash:
            raise DomainConflictError("admitted evidence does not match proposal project or basis")
        legacy = to_legacy_domain_evidence_binding(modern)
        previous = bindings.get(legacy.id)
        if previous is not None and previous != legacy:
            raise DomainConflictError("admitted evidence identity has conflicting content")
        bindings[legacy.id] = legacy
    return tuple(bindings[item] for item in sorted(bindings, key=str))


def _manifest_evidence_refs(manifest: dict[object, object]) -> tuple[str, ...]:
    return _coerce_evidence_refs(manifest.get("evidence_refs"))


def _row_evidence_refs(row: dict[object, object]) -> tuple[str, ...]:
    return _coerce_evidence_refs(row.get("evidence_refs"))


def _optional_row_evidence_refs(row: dict[object, object]) -> tuple[str, ...]:
    value = row.get("evidence_refs")
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise DomainConflictError("adaptive proposal evidence references are invalid")
    refs = tuple(value)
    if refs != tuple(sorted(set(refs))):
        raise DomainConflictError("adaptive proposal evidence references must be sorted and unique")
    return refs


def _coerce_evidence_refs(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise DomainConflictError("proposal evidence references are missing or invalid")
    refs = tuple(value)
    if refs != tuple(sorted(set(refs))):
        raise DomainConflictError("proposal evidence references must be sorted and unique")
    return refs
