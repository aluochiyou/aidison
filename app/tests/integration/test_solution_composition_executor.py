from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection, sql
from sqlalchemy import text

from aidison.application.service import ProjectApplication
from aidison.application.solution_decision_bridge import SolutionDecisionBridge
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.runtime.agent_runs import AgentRun, AgentRunKind
from aidison.runtime.checkpointing import (
    CheckpointRuntime,
    CheckpointSettings,
    normalize_psycopg_url,
)
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.runtime.minimal_graph import thread_config
from aidison.solution.composition_executor import (
    SolutionCompositionExecutor,
    SolutionCompositionMaterial,
    SolutionVerifierEvidenceContext,
)
from aidison.solution.contracts import SolutionContract, SolutionRiskClass
from aidison.solution.draft_admission import ApprovedModuleCandidate
from aidison.solution.proposal_manifest import SolutionProposalFreezer, SolutionProposalManifest
from aidison.solution.single_task_graph import build_single_task_solution_graph

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class _Composer:
    def __init__(self, *, module_id: UUID, candidate_id: UUID, evidence_id: UUID) -> None:
        self._raw_json = json.dumps(
            {
                "elements": [
                    {
                        "element_key": "power.pack",
                        "module_id": str(module_id),
                        "responsibility": "Supply bounded power.",
                        "selected_candidate_id": str(candidate_id),
                        "configuration": {"cell_count": 3},
                        "evidence_binding_ids": [str(evidence_id)],
                    }
                ],
                "interfaces": [],
            }
        )
        self.call_count = 0

    async def compose(self, *, instruction: str, input_refs: tuple[str, ...]) -> str:
        self.call_count += 1
        assert instruction == "Compose the bounded power solution."
        assert input_refs == ("artifact://solution-contract/1",)
        return self._raw_json


def _run(project_id: UUID, basis_hash: str) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.SOLUTION,
        idempotency_key=f"solution-composition-{uuid4()}",
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
        thread_id=f"solution-composition-{uuid4()}",
        run_contract_ref="artifact://solution-contract/1",
    )


@pytest.mark.asyncio
async def test_solution_executor_persists_raw_then_admits_normalized_result(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    schema_name = f"aidison_solution_graph_test_{uuid4().hex}"
    checkpoint_runtime = CheckpointRuntime(
        CheckpointSettings(
            database_url=database_url,
            schema_name=schema_name,
            min_pool_size=1,
            max_pool_size=1,
        )
    )
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Solution executor fixture",
                goal="Verify normalized solution proposal execution",
                idempotency_key=f"project:{uuid4()}",
            )
            basis_hash = _hash("basis")
            run = _run(project.id, basis_hash)
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="solution-executor", lease_seconds=60)
            assert claim is not None
            await session.commit()

        module_id, candidate_id, evidence_id = uuid4(), uuid4(), uuid4()
        contract = SolutionContract(
            solution_run_id=run.id,
            project_id=project.id,
            basis_hash=basis_hash,
            basis_project_revision=1,
            requirement_refs=("requirement://1",),
            module_revision_refs=(f"module://{module_id}",),
            accepted_decision_refs=("decision://1/option/approve",),
            admitted_evidence_refs=(f"evidence-binding://{evidence_id}",),
            constraint_set_ref="artifact://constraints/1",
            interface_contract_refs=(),
            coverage_contract_ref="artifact://coverage/1",
            risk_class=SolutionRiskClass.STANDARD,
            allowed_tool_ids=(),
            budget_ref=f"budget://agent-run/{run.id}",
            verification_policy_ref="policy://verification/standard",
            completion_policy_ref="policy://completion/1",
        )
        task = TaskEnvelope(
            run_id=run.id,
            task_key="solution.compose",
            basis_hash=basis_hash,
            plan_revision=1,
            capability="solution_composer",
            input_refs=(run.run_contract_ref,),
            dependency_task_ids=(),
            coverage_keys=("solution.power.composition",),
            allowed_tool_ids=(),
            budget_ref=f"budget://agent-run/{run.id}",
            idempotency_key=f"task:{run.id}:solution.compose",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://solution/1",
            idempotency_prefix="solution.compose",
        )
        composer = _Composer(
            module_id=module_id,
            candidate_id=candidate_id,
            evidence_id=evidence_id,
        )
        executor = SolutionCompositionExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            composer=composer,
        )
        graph = build_single_task_solution_graph(
            checkpointer=await checkpoint_runtime.start(),
            executor=executor,
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            material=SolutionCompositionMaterial(
                contract=contract,
                approved_candidates=(
                    ApprovedModuleCandidate(module_id=module_id, candidate_id=candidate_id),
                ),
                approved_evidence_ids=(evidence_id,),
                instruction="Compose the bounded power solution.",
            ),
            proposal_freezer=SolutionProposalFreezer(
                session_factory=factory,
                artifact_root=tmp_path,
            ),
        )
        state = await graph.ainvoke(
            {"run_id": str(run.id)},
            thread_config(thread_id=run.thread_id),
        )

        assert composer.call_count == 1
        assert state["raw_artifact_ref"].startswith("artifact+sha256://")
        assert state["normalized_artifact_ref"].startswith("artifact+sha256://")
        assert state["integration_artifact_ref"].startswith("artifact+sha256://")
        assert state["proposal_manifest_ref"].startswith("artifact+sha256://")
        assert state["admitted_result_ref"].startswith("admitted://agent-run-results/")
        assert state["proposal_readiness"] == "ready"
        async with factory() as session:
            payload = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=project.id,
                basis_hash=run.basis_hash,
                ref=state["proposal_manifest_ref"],
                expected_kind="solution_proposal_manifest",
            )
        manifest = SolutionProposalManifest.model_validate(payload)
        assert manifest.readiness.value == "ready"
        assert manifest.can_create_solution_version is True
        decision, checkpoint = await SolutionDecisionBridge(
            session_factory=factory,
            artifact_root=tmp_path,
        ).prepare_from_interrupt(run=run, claim=claim, graph=graph)
        assert decision.proposal_manifest_ref == state["proposal_manifest_ref"]
        assert checkpoint.generation == claim.generation
        async with factory() as session:
            waiting = await AgentRunControl(session).get(run.id)
        assert waiting is not None
        assert waiting.status.value == "waiting"
    finally:
        await checkpoint_runtime.close()
        async with await AsyncConnection.connect(
            normalize_psycopg_url(database_url), autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_elevated_solution_executes_admitted_interface_verifier_before_final_projection(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)

    class _ElevatedComposer:
        def __init__(
            self,
            *,
            producer_module_id: UUID,
            consumer_module_id: UUID,
            producer_candidate_id: UUID,
            consumer_candidate_id: UUID,
            evidence_id: UUID,
        ) -> None:
            self._raw_json = json.dumps(
                {
                    "elements": [
                        {
                            "element_key": "power.source",
                            "module_id": str(producer_module_id),
                            "responsibility": "Produce bounded electrical power.",
                            "selected_candidate_id": str(producer_candidate_id),
                            "configuration": {"cell_count": 3},
                            "output_interface_keys": ["power.feed"],
                            "evidence_binding_ids": [str(evidence_id)],
                        },
                        {
                            "element_key": "power.load",
                            "module_id": str(consumer_module_id),
                            "responsibility": "Consume bounded electrical power.",
                            "selected_candidate_id": str(consumer_candidate_id),
                            "configuration": {"max_current": 12},
                            "input_interface_keys": ["power.feed"],
                            "evidence_binding_ids": [str(evidence_id)],
                        },
                    ],
                    "interfaces": [
                        {
                            "interface_key": "power.feed",
                            "producer_module_id": str(producer_module_id),
                            "consumer_module_id": str(consumer_module_id),
                            "interface_type": "electrical",
                            "schema_or_unit": "V",
                            "direction": "source_to_load",
                            "range_or_capacity": "11.1-12.6 V; 20 A continuous",
                            "quantity_constraints": [
                                {
                                    "metric_key": "voltage",
                                    "producer_unit": "V",
                                    "consumer_unit": "V",
                                    "producer_minimum": "11.1",
                                    "producer_maximum": "12.6",
                                    "consumer_minimum": "11.1",
                                    "consumer_maximum": "12.6",
                                }
                            ],
                            "evidence_binding_ids": [str(evidence_id)],
                        }
                    ],
                }
            )

        async def compose(self, *, instruction: str, input_refs: tuple[str, ...]) -> str:
            assert instruction == "Compose an elevated bounded power solution."
            assert input_refs == ("artifact://solution-contract/elevated",)
            return self._raw_json

    class _ConfirmingVerifier:
        def __init__(self) -> None:
            self.instructions: list[str] = []

        async def verify(self, *, instruction: str) -> str:
            self.instructions.append(instruction)
            request = json.loads(instruction)
            return json.dumps(
                {
                    "interface_id": request["interface"]["id"],
                    "outcome": "confirmed",
                    "summary": "The bounded evidence supports the assigned interface.",
                    "evidence_refs": [request["allowed_evidence"][0]["evidence_ref"]],
                }
            )

    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Elevated solution verifier fixture",
                goal="Verify a bounded electrical interface before proposal readiness.",
                idempotency_key=f"project:{uuid4()}",
            )
            basis_hash = _hash("elevated-basis")
            run = _run(project.id, basis_hash)
            run = run.model_copy(
                update={"run_contract_ref": "artifact://solution-contract/elevated"}
            )
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(
                worker_id="elevated-solution-executor", lease_seconds=60
            )
            assert claim is not None
            await session.commit()

        producer_module_id, consumer_module_id = uuid4(), uuid4()
        producer_candidate_id, consumer_candidate_id, evidence_id = uuid4(), uuid4(), uuid4()
        contract = SolutionContract(
            solution_run_id=run.id,
            project_id=project.id,
            basis_hash=basis_hash,
            basis_project_revision=1,
            requirement_refs=("requirement://1",),
            module_revision_refs=(
                f"module://{producer_module_id}",
                f"module://{consumer_module_id}",
            ),
            accepted_decision_refs=("decision://1/option/approve",),
            admitted_evidence_refs=(f"evidence-binding://{evidence_id}",),
            constraint_set_ref="artifact://constraints/1",
            interface_contract_refs=(),
            coverage_contract_ref="artifact://coverage/1",
            risk_class=SolutionRiskClass.ELEVATED,
            allowed_tool_ids=("web.read",),
            budget_ref=f"budget://agent-run/{run.id}",
            verification_policy_ref="policy://verification/elevated",
            completion_policy_ref="policy://completion/1",
        )
        task = TaskEnvelope(
            run_id=run.id,
            task_key="solution.compose",
            basis_hash=basis_hash,
            plan_revision=1,
            capability="solution_composer",
            input_refs=(run.run_contract_ref,),
            dependency_task_ids=(),
            coverage_keys=("solution.power.composition",),
            allowed_tool_ids=("web.read",),
            budget_ref=contract.budget_ref,
            idempotency_key=f"task:{run.id}:solution.compose",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://solution/elevated",
            idempotency_prefix="solution.compose",
        )
        verifier = _ConfirmingVerifier()
        execution = await SolutionCompositionExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            composer=_ElevatedComposer(
                producer_module_id=producer_module_id,
                consumer_module_id=consumer_module_id,
                producer_candidate_id=producer_candidate_id,
                consumer_candidate_id=consumer_candidate_id,
                evidence_id=evidence_id,
            ),
            verifier=verifier,
        ).execute(
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            material=SolutionCompositionMaterial(
                contract=contract,
                approved_candidates=(
                    ApprovedModuleCandidate(
                        module_id=producer_module_id,
                        candidate_id=producer_candidate_id,
                    ),
                    ApprovedModuleCandidate(
                        module_id=consumer_module_id,
                        candidate_id=consumer_candidate_id,
                    ),
                ),
                approved_evidence_ids=(evidence_id,),
                verifier_evidence_context=(
                    SolutionVerifierEvidenceContext(
                        evidence_ref=f"evidence-binding://{evidence_id}",
                        claim="The documented pack supports the bounded output.",
                        source_url="https://example.test/power-pack",
                        span_text="11.1-12.6 V; 20 A continuous.",
                    ),
                ),
                instruction="Compose an elevated bounded power solution.",
            ),
        )

        assert len(verifier.instructions) == 1
        assert execution.integration.outcome.value == "passed"
        assert execution.elements.interfaces[0].verification_status.value == "verified"
        assert len(execution.verifier_observation_refs) == 1
        async with factory() as session:
            assert len(await AgentResultStore(session).admitted_task_ids(run_id=run.id)) == 2
    finally:
        await engine.dispose()
