from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.application.langgraph_worker import LangGraphOrchestrationWorker
from aidison.application.service import ProjectApplication
from aidison.application.solution_run_execution import SolutionLangGraphWorker, SolutionRunExecutor
from aidison.domain.models import Candidate, DecisionOption, EvidenceBinding, EvidenceStatus
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.graphs import GraphRegistry, RegisteredGraph
from aidison.solution.contracts import SolutionContract, SolutionCoverageContract

pytestmark = pytest.mark.integration


def _module_discovery_model() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "Solution needs one bounded module.",
                    "modules": [
                        {
                            "key": "power",
                            "name": "Power",
                            "responsibility": "Supply a bounded electrical source",
                        }
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


class _SolutionComposer:
    def __init__(self, *, module_id: UUID, candidate_id: UUID, evidence_id: UUID) -> None:
        self._candidate_id = candidate_id
        self._raw_json = json.dumps(
            {
                "elements": [
                    {
                        "element_key": "power.pack",
                        "module_id": str(module_id),
                        "responsibility": "Supply the selected bounded power pack.",
                        "selected_candidate_id": str(candidate_id),
                        "configuration": {"cell_count": 3},
                        "evidence_binding_ids": [str(evidence_id)],
                    }
                ],
                "interfaces": [],
            }
        )

    async def compose(self, *, instruction: str, input_refs: tuple[str, ...]) -> str:
        material = json.loads(instruction)
        assert material["approved_modules"][0]["key"] == "power"
        assert material["approved_modules"][0]["selected_candidate"]["candidate_id"] == str(
            self._candidate_id
        )
        assert len(input_refs) == 2
        return self._raw_json


@pytest.mark.asyncio
async def test_approved_evidence_bound_decision_creates_one_ref_only_solution_run(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        key = str(uuid4())
        api = create_app(
            factory,
            artifact_root=tmp_path,
            module_discovery_model_factory=_module_discovery_model,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            project_response = await client.post(
                "/api/projects",
                json={"name": "Solution run fixture", "goal": "Build a bounded power solution"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert project_response.status_code == 201, project_response.text
            project_id = UUID(project_response.json()["id"])
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={"goal": "Build a bounded power solution"},
                headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
            )
            assert requirements.status_code == 200, requirements.text
            discovery = await client.post(
                f"/api/projects/{project_id}/module-discovery",
                headers={"Idempotency-Key": f"{key}:discover", "If-Match": '"2"'},
            )
            assert discovery.status_code == 201, discovery.text
            reshape = discovery.json()["reshape_proposal"]
            applied = await client.post(
                f"/api/reshape-proposals/{reshape['id']}/resolve",
                json={"decision": "applied"},
                headers={"Idempotency-Key": f"{key}:apply", "If-Match": '"2"'},
            )
            assert applied.status_code == 200, applied.text

        async with factory() as session:
            store = PostgresDomainStore(session)
            project = await store.get_project(project_id)
            assert project is not None
            modules = await store.list_modules(project_id)
            assert len(modules) == 1
            module = modules[0]
            evidence = EvidenceBinding(
                project_id=project_id,
                module_id=module.id,
                claim="The selected pack provides the documented output range.",
                source_url="https://example.test/power-pack",
                snapshot_hash=sha256(b"power-pack").hexdigest(),
                span_text="Documented electrical range.",
                status=EvidenceStatus.SUPPORTED,
                observed_at=datetime.now(UTC),
            )
            candidate = Candidate(
                project_id=project_id,
                module_id=module.id,
                name="Documented power pack",
                description="An evidence-bound power candidate.",
                evidence_binding_ids=(evidence.id,),
            )
            decision = await ProjectApplication(store).submit_research_proposal(
                project_id=project_id,
                expected_project_revision=project.revision,
                evidence=(evidence,),
                candidates=(candidate,),
                findings=(),
                decision_question="Use the documented power pack?",
                decision_options=(
                    DecisionOption(
                        option_id="approve",
                        label="Use documented pack",
                        summary="Use the evidence-bound power candidate.",
                        candidate_ids=(candidate.id,),
                        evidence_binding_ids=(evidence.id,),
                    ),
                    DecisionOption(
                        option_id="alternate",
                        label="Use alternate pack",
                        summary="Select an alternative and research again.",
                        candidate_ids=(candidate.id,),
                        evidence_binding_ids=(evidence.id,),
                    ),
                ),
                idempotency_key=f"{key}:research",
            )
            project = await store.get_project(project_id)
            assert project is not None
            approved = await ProjectApplication(store).resolve_decision(
                decision_id=decision.id,
                expected_project_revision=project.revision,
                selected_option_id="approve",
                basis_hash=decision.basis_hash,
                idempotency_key=f"{key}:decision",
            )
            project = await store.get_project(project_id)
            assert project is not None
            queued_revision = project.revision

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            first = await client.post(
                f"/api/projects/{project_id}/agent-runs/solution",
                json={"decision_id": str(approved.id), "risk_class": "standard"},
                headers={
                    "Idempotency-Key": f"{key}:solution-run",
                    "If-Match": f'"{queued_revision}"',
                },
            )
            replayed = await client.post(
                f"/api/projects/{project_id}/agent-runs/solution",
                json={"decision_id": str(approved.id), "risk_class": "standard"},
                headers={
                    "Idempotency-Key": f"{key}:solution-run-retry",
                    "If-Match": f'"{queued_revision}"',
                },
            )
        assert first.status_code == 202, first.text
        assert replayed.status_code == 202, replayed.text
        assert first.json()["agent_run"]["id"] == replayed.json()["agent_run"]["id"]
        async with factory() as session:
            run = await AgentRunControl(session).get(UUID(first.json()["agent_run"]["id"]))
            assert run is not None
            assert run.run_contract_ref is not None
            assert run.coverage_contract_ref is not None

            artifacts = ContentAddressedArtifactStore(session, tmp_path)
            contract_payload = await artifacts.read_json_ref(
                project_id=project_id,
                basis_hash=run.basis_hash,
                ref=run.run_contract_ref,
                expected_kind="solution_contract",
            )
            coverage_payload = await artifacts.read_json_ref(
                project_id=project_id,
                basis_hash=run.basis_hash,
                ref=run.coverage_contract_ref,
                expected_kind="solution_coverage_contract",
            )

        contract = SolutionContract.model_validate(contract_payload)
        coverage = SolutionCoverageContract.model_validate(coverage_payload)
        assert contract.solution_run_id == run.id
        assert contract.accepted_decision_refs == (f"decision://{approved.id}/option/approve",)
        assert contract.admitted_evidence_refs == (f"evidence-binding://{evidence.id}",)
        assert coverage.basis_hash == run.basis_hash
        assert len(coverage.keys) == 1

        executor = SolutionRunExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            checkpointer=InMemorySaver(),
            composer=_SolutionComposer(
                module_id=module.id,
                candidate_id=candidate.id,
                evidence_id=evidence.id,
            ),
        )
        execution = await SolutionLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=factory,
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=run.runtime_binding,
                            compiled_graph=executor,
                        ),
                    )
                ),
                worker_id="solution-run-application",
            ),
        ).run_once()
        assert execution is not None
        assert execution.readiness == "ready"
        assert execution.agent_decision is not None
        agent_decision = execution.agent_decision
        async with factory() as session:
            project = await PostgresDomainStore(session).get_project(project_id)
            assert project is not None
            expected_revision = project.revision
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            resolved = await client.post(
                f"/api/agent-run-decisions/{agent_decision.id}/resolve",
                json={"decision": "approved", "basis_hash": run.basis_hash},
                headers={
                    "Idempotency-Key": f"{key}:solution-approve",
                    "If-Match": f'"{expected_revision}"',
                },
            )
        assert resolved.status_code == 200, resolved.text
        payload = resolved.json()
        assert payload["solution"]["solution_proposal_id"] is not None
        assert payload["project_revision"] == expected_revision + 2
        async with factory() as session:
            completed = await AgentRunControl(session).get(run.id)
            assert completed is not None
            assert completed.status.value == "succeeded"
            versions = await PostgresDomainStore(session).list_solution_versions(project_id)
            assert len(versions) == 1
            assert versions[0].dependency_projection_complete is True
            assert versions[0].dependencies == ()
            assert versions[0].bom == (
                {
                    "line_id": f"bom-{module.id.hex}",
                    "module_id": str(module.id),
                    "candidate_id": str(candidate.id),
                    "name": candidate.name,
                    "quantity": 1.0,
                    "unit": "item",
                    "evidence_binding_ids": [str(evidence.id)],
                },
            )
    finally:
        await engine.dispose()
