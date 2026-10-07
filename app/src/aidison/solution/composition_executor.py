"""One bounded Solution capability execution with durable raw-output audit order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import session_scope
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ExecutionGrant,
    ResearchResultStatus,
    ResultEnvelope,
    TaskEnvelope,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim
from aidison.solution.contracts import SolutionContract
from aidison.solution.draft_admission import (
    ApprovedModuleCandidate,
    SolutionDraftAdmissionInput,
    validate_solution_draft_material,
)
from aidison.solution.elements import InterfaceContract, SolutionElementSet
from aidison.solution.execution import SolutionRunCancelled
from aidison.solution.integration import IntegrationCheckReport, check_solution_integration
from aidison.solution.normalize import (
    SolutionNormalizationArtifactRefs,
    SolutionNormalizationInput,
    normalize_solution_composition,
)
from aidison.solution.payloads import SolutionCompositionPayload
from aidison.solution.verification import (
    AdmittedInterfaceVerifierObservation,
    SolutionInterfaceVerifierTask,
    apply_admitted_interface_verifier_observations,
    route_solution_verifier_tasks,
)
from aidison.solution.verifier import InterfaceVerifier
from aidison.solution.verifier_execution import InterfaceVerifierExecutor


class SolutionComposer(Protocol):
    """One physical solution-composition call; provider retries belong outside this seam."""

    async def compose(self, *, instruction: str, input_refs: tuple[str, ...]) -> str: ...


class SolutionVerifierEvidenceContext(BaseModel):
    """Small, bounded evidence context available to an independent verifier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_ref: str = Field(min_length=1, max_length=500)
    claim: str = Field(min_length=1, max_length=2_000)
    source_url: str = Field(min_length=1, max_length=2_000)
    span_text: str = Field(min_length=1, max_length=4_000)


class SolutionCompositionMaterial(BaseModel):
    """Trusted, frozen material provided to one private Solution capability invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: SolutionContract
    approved_candidates: tuple[ApprovedModuleCandidate, ...] = Field(min_length=1, max_length=64)
    approved_evidence_ids: tuple[UUID, ...] = Field(max_length=256)
    verifier_evidence_context: tuple[SolutionVerifierEvidenceContext, ...] = Field(
        default=(), max_length=256
    )
    instruction: str = Field(min_length=1, max_length=12_000)


class SolutionCompositionExecution(BaseModel):
    """Only typed references and normalized proposal data leave this execution seam."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_artifact_ref: str
    normalized_artifact_ref: str
    integration_artifact_ref: str
    verifier_plan_artifact_ref: str
    verifier_observation_refs: tuple[str, ...] = Field(default=(), max_length=32)
    elements: SolutionElementSet
    integration: IntegrationCheckReport
    result: ResultEnvelope
    admission: AdmissionRecord


class SolutionCompositionExecutor:
    """Persist raw output before validation, then admit one normalized result at a safe point."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
        composer: SolutionComposer,
        verifier: InterfaceVerifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._composer = composer
        self._verifier = verifier

    async def execute(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        material: SolutionCompositionMaterial,
    ) -> SolutionCompositionExecution:
        if task.run_id != run.id or grant.task_id != task.id:
            raise ValueError(
                "solution task and execution grant must belong to the claimed AgentRun"
            )
        if task.basis_hash != run.basis_hash or grant.generation != claim.generation:
            raise ValueError("solution task/grant does not match frozen run basis or generation")
        if (
            material.contract.solution_run_id != run.id
            or material.contract.project_id != run.project_id
        ):
            raise ValueError("solution material contract does not belong to the claimed AgentRun")
        if material.contract.basis_hash != run.basis_hash:
            raise ValueError("solution material contract does not match frozen run basis")
        if run.run_contract_ref is None or run.run_contract_ref not in task.input_refs:
            raise ValueError("solution task must explicitly reference the pinned run contract")

        await self._acknowledge_pending_cancellation(claim=claim)
        raw_json = await self._composer.compose(
            instruction=material.instruction,
            input_refs=task.input_refs,
        )
        async with session_scope(self._session_factory) as session:
            raw = await ContentAddressedArtifactStore(
                session, self._artifact_root
            ).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="solution_raw_output",
                value={"raw_json": raw_json},
            )

        payload = SolutionCompositionPayload.model_validate_json(raw_json)
        admitted_payload = validate_solution_draft_material(
            SolutionDraftAdmissionInput(
                contract=material.contract,
                approved_candidates=material.approved_candidates,
                approved_evidence_ids=material.approved_evidence_ids,
                payload=payload,
            )
        )
        artifact_refs = await self._write_element_artifacts(
            run=run,
            payload=admitted_payload,
        )
        elements = normalize_solution_composition(
            SolutionNormalizationInput(
                contract=material.contract,
                payload=admitted_payload,
                artifact_refs=artifact_refs,
            )
        )
        integration = check_solution_integration(elements)
        verifier_plan = route_solution_verifier_tasks(
            contract=material.contract,
            elements=elements,
            integration=integration,
        )
        verifier_observation_refs: tuple[str, ...] = ()
        if verifier_plan.tasks and self._verifier is not None:
            observations = await self._execute_verifier_tasks(
                run=run,
                claim=claim,
                material=material,
                verifier_tasks=verifier_plan.tasks,
                elements=elements,
            )
            projection = apply_admitted_interface_verifier_observations(
                elements=elements,
                observations=observations,
            )
            elements = projection.elements
            integration = check_solution_integration(elements)
            verifier_observation_refs = projection.admitted_result_refs
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            normalized = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="solution_normalized_elements",
                value=elements.model_dump(mode="json"),
            )
            integration_artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="solution_integration_check",
                value=integration.model_dump(mode="json"),
            )
            verifier_plan_artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="solution_verifier_task_plan",
                value=verifier_plan.model_dump(mode="json"),
            )

        result = ResultEnvelope(
            run_id=run.id,
            task_id=task.id,
            basis_hash=run.basis_hash,
            producer_attempt_id=grant.attempt_id,
            producer_generation=grant.generation,
            producer_profile_ref=run.runtime_binding.profile_binding_ref,
            status=ResearchResultStatus.SUCCEEDED,
            artifact_ref=normalized.ref,
            manifest_hash=normalized.content_hash,
            evidence_refs=material.contract.admitted_evidence_refs,
            coverage_observation_refs=(),
            unresolved_refs=tuple(
                item for element in elements.elements for item in element.unresolved_refs
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
                "material_authorized",
                "artifact_present",
            ),
            admitted_ref=f"admitted://agent-run-results/{result.id}",
        )
        cancelled_at_safe_point = False
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            if active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_at_safe_point = True
            else:
                results = AgentResultStore(session)
                await results.record_result(result)
                await results.admit(admission)
        if cancelled_at_safe_point:
            raise SolutionRunCancelled(
                "AgentRun cancellation acknowledged at solution result-admission safe point"
            )
        return SolutionCompositionExecution(
            raw_artifact_ref=raw.ref,
            normalized_artifact_ref=normalized.ref,
            integration_artifact_ref=integration_artifact.ref,
            verifier_plan_artifact_ref=verifier_plan_artifact.ref,
            verifier_observation_refs=verifier_observation_refs,
            elements=elements,
            integration=integration,
            result=result,
            admission=admission,
        )

    async def _write_element_artifacts(
        self,
        *,
        run: AgentRun,
        payload: SolutionCompositionPayload,
    ) -> tuple[SolutionNormalizationArtifactRefs, ...]:
        """Persist model configuration/unknown text before converting it to public refs."""

        refs: list[SolutionNormalizationArtifactRefs] = []
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            for element in sorted(payload.elements, key=lambda item: item.element_key):
                configuration = await artifacts.put_agent_run_json(
                    project_id=run.project_id,
                    agent_run_id=run.id,
                    basis_hash=run.basis_hash,
                    kind="solution_element_configuration",
                    value={
                        "element_key": element.element_key,
                        "configuration": element.configuration,
                    },
                )
                unresolved_ref: str | None = None
                if element.unresolved:
                    unresolved = await artifacts.put_agent_run_json(
                        project_id=run.project_id,
                        agent_run_id=run.id,
                        basis_hash=run.basis_hash,
                        kind="solution_element_unresolved",
                        value={"element_key": element.element_key, "unknowns": element.unresolved},
                    )
                    unresolved_ref = unresolved.ref
                refs.append(
                    SolutionNormalizationArtifactRefs(
                        element_key=element.element_key,
                        configuration_ref=configuration.ref,
                        unresolved_ref=unresolved_ref,
                    )
                )
        return tuple(refs)

    async def _execute_verifier_tasks(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        material: SolutionCompositionMaterial,
        verifier_tasks: tuple[SolutionInterfaceVerifierTask, ...],
        elements: SolutionElementSet,
    ) -> tuple[AdmittedInterfaceVerifierObservation, ...]:
        """Run bounded verifier tasks sequentially under the existing Run claim.

        They are intentionally not a second scheduler: each task has a stable
        envelope and an independent admission, but this first SolutionGraph
        vertical slice has no parallel internal ready-set yet.
        """

        if self._verifier is None:  # pragma: no cover - guarded by the caller
            return ()
        interfaces = {item.id: item for item in elements.interfaces}
        executor = InterfaceVerifierExecutor(
            session_factory=self._session_factory,
            artifact_root=self._artifact_root,
            verifier=self._verifier,
        )
        observations: list[AdmittedInterfaceVerifierObservation] = []
        for verifier_task in sorted(verifier_tasks, key=lambda item: item.task_key):
            interface = interfaces.get(verifier_task.interface_id)
            if interface is None:
                raise ValueError("verifier task references an interface outside the composition")
            envelope = TaskEnvelope(
                run_id=run.id,
                task_key=verifier_task.task_key,
                basis_hash=run.basis_hash,
                plan_revision=1,
                capability="solution_interface_verifier",
                input_refs=verifier_task.input_refs,
                dependency_task_ids=(),
                coverage_keys=(f"solution.interface.{interface.id}.verification",),
                allowed_tool_ids=verifier_task.allowed_tool_ids,
                budget_ref=verifier_task.budget_ref,
                idempotency_key=f"solution.verifier:{run.id}:{interface.id}",
            )
            grant = ExecutionGrant(
                task_id=envelope.id,
                attempt_id=uuid4(),
                generation=claim.generation,
                lease_token=claim.lease_token,
                deadline_ref=f"deadline://agent-run/{run.id}/interface/{interface.id}",
                idempotency_prefix=envelope.idempotency_key,
            )
            execution = await executor.execute(
                run=run,
                claim=claim,
                task=envelope,
                grant=grant,
                verifier_task=verifier_task,
                instruction=self._verifier_instruction(
                    interface=interface,
                    verifier_task=verifier_task,
                    evidence_context=material.verifier_evidence_context,
                ),
            )
            if execution.admission.admitted_ref is None:  # pragma: no cover - contract guarded
                raise RuntimeError("accepted verifier result is missing its admitted reference")
            observations.append(
                AdmittedInterfaceVerifierObservation(
                    result_ref=execution.admission.admitted_ref,
                    payload=execution.payload,
                )
            )
        return tuple(observations)

    @staticmethod
    def _verifier_instruction(
        *,
        interface: InterfaceContract,
        verifier_task: SolutionInterfaceVerifierTask,
        evidence_context: tuple[SolutionVerifierEvidenceContext, ...],
    ) -> str:
        contexts_by_ref = {item.evidence_ref: item for item in evidence_context}
        if len(contexts_by_ref) != len(evidence_context):
            raise ValueError("verifier evidence context references must be unique")
        missing_refs = set(verifier_task.input_refs) - set(contexts_by_ref)
        if missing_refs:
            raise ValueError("verifier task is missing bounded evidence context")
        return json.dumps(
            {
                "task": (
                    "Assess only whether the allowed evidence supports this exact interface. "
                    "Return InterfaceVerifierPayload JSON; do not propose a design change."
                ),
                "interface": interface.model_dump(mode="json"),
                "allowed_evidence": [
                    contexts_by_ref[item].model_dump(mode="json")
                    for item in verifier_task.input_refs
                ],
                "reason_codes": verifier_task.reason_codes,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    async def _acknowledge_pending_cancellation(self, *, claim: AgentRunClaim) -> None:
        """Cancel before a provider call; a second gate exists immediately before admission."""

        cancelled_before_dispatch = False
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            if active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_before_dispatch = True
        if cancelled_before_dispatch:
            raise SolutionRunCancelled(
                "AgentRun cancellation acknowledged before solution composer dispatch"
            )


__all__ = [
    "SolutionComposer",
    "SolutionCompositionExecution",
    "SolutionCompositionExecutor",
    "SolutionCompositionMaterial",
    "SolutionRunCancelled",
    "SolutionVerifierEvidenceContext",
]
