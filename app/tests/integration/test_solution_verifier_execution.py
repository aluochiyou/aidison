from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.runtime.agent_runs import AgentRun, AgentRunKind
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.solution.verification import SolutionInterfaceVerifierTask
from aidison.solution.verifier import JsonModeInterfaceVerifier
from aidison.solution.verifier_execution import InterfaceVerifierExecutor

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _run(*, project_id: UUID, basis_hash: str) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.SOLUTION,
        idempotency_key=f"solution-verifier-{uuid4()}",
        basis_hash=basis_hash,
        basis_project_revision=1,
        runtime_binding=RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="solution",
            graph_revision="solution-v1",
            state_schema_version="solution-state-v1",
            profile_binding_ref="profile://solution/1",
            policy_binding_ref="policy://solution/1",
        ),
        thread_id=f"solution-verifier-{uuid4()}",
    )


@pytest.mark.asyncio
async def test_interface_verifier_persists_raw_then_admits_scoped_observation(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Interface verifier fixture",
                goal="Verify one bounded electrical interface.",
                idempotency_key=f"project:{uuid4()}",
            )
            run = _run(project_id=project.id, basis_hash=_hash("verifier-basis"))
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="interface-verifier", lease_seconds=60)
            assert claim is not None
            await session.commit()

        interface_id = uuid4()
        verifier_task = SolutionInterfaceVerifierTask(
            run_id=run.id,
            project_id=project.id,
            basis_hash=run.basis_hash,
            interface_id=interface_id,
            task_key=f"solution.verify.{interface_id}",
            input_refs=("evidence-binding://allowed",),
            allowed_tool_ids=(),
            budget_ref=f"budget://agent-run/{run.id}",
            reason_codes=("interface_needs_verification",),
            idempotency_hash=_hash("verifier-task"),
        )
        envelope = TaskEnvelope(
            run_id=run.id,
            task_key=verifier_task.task_key,
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="solution_interface_verifier",
            input_refs=verifier_task.input_refs,
            dependency_task_ids=(),
            coverage_keys=("solution.power.interface",),
            allowed_tool_ids=(),
            budget_ref=verifier_task.budget_ref,
            idempotency_key=f"verifier:{run.id}:{interface_id}",
        )
        grant = ExecutionGrant(
            task_id=envelope.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref=f"deadline://agent-run/{run.id}",
            idempotency_prefix=envelope.idempotency_key,
        )
        verifier = MagicMock(spec=JsonModeInterfaceVerifier)
        verifier.verify = AsyncMock(
            return_value=json.dumps(
                {
                    "interface_id": str(interface_id),
                    "outcome": "confirmed",
                    "summary": "The approved source covers the bounded voltage interface.",
                    "evidence_refs": ["evidence-binding://allowed"],
                }
            )
        )
        execution = await InterfaceVerifierExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            verifier=verifier,
        ).execute(
            run=run,
            claim=claim,
            task=envelope,
            grant=grant,
            verifier_task=verifier_task,
            instruction="Verify only the assigned interface.",
        )

        assert execution.admission.result_id == execution.result.id
        assert execution.admission.admitted_ref is not None
        verifier.verify.assert_awaited_once_with(instruction="Verify only the assigned interface.")
        async with factory() as session:
            artifacts = ContentAddressedArtifactStore(session, tmp_path)
            raw = await artifacts.list_metadata(
                project_id=project.id,
                kind="solution_interface_verifier_raw_output",
            )
            observations = await artifacts.list_metadata(
                project_id=project.id,
                kind="solution_interface_verifier_observation",
            )
            assert len(raw) == 1
            assert len(observations) == 1
            assert raw[0].agent_run_id == run.id
            assert observations[0].content_hash == execution.result.manifest_hash
            assert await AgentResultStore(session).admitted_task_ids(run_id=run.id) == (
                envelope.id,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_interface_verifier_keeps_raw_audit_when_payload_is_out_of_scope(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Verifier scope rejection fixture",
                goal="Reject a verifier observation outside its assigned interface.",
                idempotency_key=f"project:{uuid4()}",
            )
            run = _run(project_id=project.id, basis_hash=_hash("scope-rejection"))
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="interface-verifier", lease_seconds=60)
            assert claim is not None
            await session.commit()

        interface_id = uuid4()
        verifier_task = SolutionInterfaceVerifierTask(
            run_id=run.id,
            project_id=project.id,
            basis_hash=run.basis_hash,
            interface_id=interface_id,
            task_key=f"solution.verify.{interface_id}",
            input_refs=("evidence-binding://allowed",),
            allowed_tool_ids=(),
            budget_ref=f"budget://agent-run/{run.id}",
            reason_codes=("interface_needs_verification",),
            idempotency_hash=_hash("scope-rejection-task"),
        )
        envelope = TaskEnvelope(
            run_id=run.id,
            task_key=verifier_task.task_key,
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="solution_interface_verifier",
            input_refs=verifier_task.input_refs,
            dependency_task_ids=(),
            coverage_keys=("solution.power.interface",),
            allowed_tool_ids=(),
            budget_ref=verifier_task.budget_ref,
            idempotency_key=f"verifier:{run.id}:{interface_id}",
        )
        grant = ExecutionGrant(
            task_id=envelope.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref=f"deadline://agent-run/{run.id}",
            idempotency_prefix=envelope.idempotency_key,
        )
        verifier = MagicMock(spec=JsonModeInterfaceVerifier)
        verifier.verify = AsyncMock(
            return_value=json.dumps(
                {
                    "interface_id": str(uuid4()),
                    "outcome": "confirmed",
                    "summary": "This is outside the assigned interface.",
                    "evidence_refs": ["evidence-binding://allowed"],
                }
            )
        )

        with pytest.raises(ValueError, match="outside its assigned interface"):
            await InterfaceVerifierExecutor(
                session_factory=factory,
                artifact_root=tmp_path,
                verifier=verifier,
            ).execute(
                run=run,
                claim=claim,
                task=envelope,
                grant=grant,
                verifier_task=verifier_task,
                instruction="Verify only the assigned interface.",
            )

        async with factory() as session:
            artifacts = ContentAddressedArtifactStore(session, tmp_path)
            raw = await artifacts.list_metadata(
                project_id=project.id,
                kind="solution_interface_verifier_raw_output",
            )
            observations = await artifacts.list_metadata(
                project_id=project.id,
                kind="solution_interface_verifier_observation",
            )
            assert len(raw) == 1
            assert observations == ()
            assert await AgentResultStore(session).admitted_task_ids(run_id=run.id) == ()
    finally:
        await engine.dispose()
