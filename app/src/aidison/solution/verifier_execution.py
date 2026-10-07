"""Durable execution boundary for one independent interface verifier task."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict
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
from aidison.solution.execution import SolutionRunCancelled
from aidison.solution.verification import SolutionInterfaceVerifierTask
from aidison.solution.verifier import (
    InterfaceVerifier,
    InterfaceVerifierPayload,
    validate_interface_verifier_payload,
)


class InterfaceVerifierExecutor:
    """Persist raw model output before typed validation and durable admission."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
        verifier: InterfaceVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._verifier = verifier

    async def execute(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        verifier_task: SolutionInterfaceVerifierTask,
        instruction: str,
    ) -> InterfaceVerifierExecution:
        if task.run_id != run.id or grant.task_id != task.id or verifier_task.run_id != run.id:
            raise ValueError("verifier task does not belong to the claimed AgentRun")
        if task.basis_hash != run.basis_hash or grant.generation != claim.generation:
            raise ValueError(
                "verifier task does not match the claimed AgentRun basis or generation"
            )
        await self._acknowledge_pending_cancellation(claim=claim)
        raw_json = await self._verifier.verify(instruction=instruction)
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            raw = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="solution_interface_verifier_raw_output",
                value={"raw_json": raw_json},
            )
        payload = validate_interface_verifier_payload(
            payload=InterfaceVerifierPayload.model_validate_json(raw_json),
            expected_interface_id=verifier_task.interface_id,
            allowed_evidence_refs=verifier_task.input_refs,
        )
        cancelled_at_safe_point = False
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            observation = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="solution_interface_verifier_observation",
                value=payload.model_dump(mode="json"),
            )
            result = ResultEnvelope(
                run_id=run.id,
                task_id=task.id,
                basis_hash=run.basis_hash,
                producer_attempt_id=grant.attempt_id,
                producer_generation=grant.generation,
                producer_profile_ref=run.runtime_binding.profile_binding_ref,
                status=ResearchResultStatus.SUCCEEDED,
                artifact_ref=observation.ref,
                manifest_hash=observation.content_hash,
                evidence_refs=payload.evidence_refs,
                coverage_observation_refs=(),
                unresolved_refs=payload.unresolved,
            )
            admission = AdmissionRecord(
                run_id=run.id,
                result_id=result.id,
                result_manifest_hash=result.manifest_hash,
                disposition=AdmissionDisposition.ACCEPTED,
                reason_codes=("runtime_fenced", "schema_valid", "interface_scope_valid"),
                admitted_ref=f"admitted://agent-run-results/{result.id}",
            )
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
                "AgentRun cancellation acknowledged at verifier result-admission safe point"
            )
        return InterfaceVerifierExecution(
            raw_artifact_ref=raw.ref,
            observation_artifact_ref=observation.ref,
            payload=payload,
            result=result,
            admission=admission,
        )

    async def _acknowledge_pending_cancellation(self, *, claim: AgentRunClaim) -> None:
        cancelled_before_dispatch = False
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            if active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_before_dispatch = True
        if cancelled_before_dispatch:
            raise SolutionRunCancelled(
                "AgentRun cancellation acknowledged before interface verifier dispatch"
            )


class InterfaceVerifierExecution(BaseModel):
    """Only durable refs and the typed observation leave one verifier invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_artifact_ref: str
    observation_artifact_ref: str
    payload: InterfaceVerifierPayload
    result: ResultEnvelope
    admission: AdmissionRecord


__all__ = ["InterfaceVerifierExecution", "InterfaceVerifierExecutor"]
