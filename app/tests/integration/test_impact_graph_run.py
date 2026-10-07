from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.application.agent_run_application import (
    DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING,
)
from aidison.application.impact_proposal_run_execution import (
    ImpactProposalGraphRunExecutor,
    ImpactProposalLangGraphWorker,
)
from aidison.application.langgraph_worker import LangGraphOrchestrationWorker
from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    Candidate,
    DecisionOption,
    DecisionRequest,
    DecisionStatus,
    EvidenceBinding,
    EvidenceStatus,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProposedModule,
    SolutionDependency,
    SolutionDependencyKind,
    SolutionVersion,
)
from aidison.impact.proposal import ImpactProposalManifest
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.graphs import GraphRegistry, RegisteredGraph

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class _ImpactAnalyst:
    def __init__(
        self,
        *,
        module_id: UUID,
        candidate_id: UUID,
        evidence_id: UUID,
        snapshot: str,
    ) -> None:
        self._payload = {
            "summary": "Replace the observed source and re-verify the consumer interface.",
            "module_patches": [
                {
                    "module_id": str(module_id),
                    "base_snapshot_hash": snapshot,
                    "candidate_id": str(candidate_id),
                    "candidate_name": "Replacement source",
                    "rationale": "The observation invalidates the original source output.",
                    "evidence_binding_ids": [str(evidence_id)],
                    "risks": ["The consumer must be re-verified."],
                }
            ],
            "replacement_bom_items": [
                {
                    "line_id": "replacement-source",
                    "module_id": str(module_id),
                    "candidate_id": str(candidate_id),
                    "name": "Replacement source",
                    "quantity": 1.0,
                    "unit": "item",
                    "evidence_binding_ids": [str(evidence_id)],
                }
            ],
            "replacement_implementation_steps": [
                {
                    "step_id": "replace-source",
                    "title": "Replace source",
                    "instruction": "Install the approved replacement source.",
                    "module_ids": [str(module_id)],
                    "acceptance": ["Source output is available."],
                }
            ],
            "replacement_verification_steps": [
                {
                    "step_id": "verify-source-consumer",
                    "title": "Verify interface",
                    "instruction": "Verify the source-to-consumer interface.",
                    "module_ids": [str(module_id)],
                    "acceptance": ["Consumer compatibility is checked."],
                }
            ],
            "stale_evidence_binding_ids": [],
            "risks": ["The consumer must be re-verified."],
            "unknowns": [],
        }

    async def analyze(self, *, instruction: str) -> str:
        assert "Permitted existing candidates" in instruction
        return json.dumps(self._payload)


@pytest.mark.asyncio
async def test_impact_graph_requires_user_approval_before_writing_the_next_solution_revision(
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
            store = PostgresDomainStore(session)
            app = ProjectApplication(store)
            project = await app.create_project(
                name="Impact graph fixture",
                goal="Build a source and dependent consumer.",
                idempotency_key=f"impact:project:{uuid4()}",
            )
            requirement, _ = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=1,
                goal=project.goal,
                hard_constraints=(),
                preferences=(),
                available_resources=(),
                unknowns=(),
                modules=(),
                idempotency_key=f"impact:requirements:{uuid4()}",
            )
            reshape = await app.propose_project_reshape(
                proposal=ProjectReshapeProposal(
                    project_id=project.id,
                    target_goal=project.goal,
                    summary="Make the producer/consumer interface explicit.",
                    new_module_keys=("source", "consumer"),
                    new_modules=(
                        ProposedModule(
                            key="source", name="Source", responsibility="Provide output."
                        ),
                        ProposedModule(
                            key="consumer",
                            name="Consumer",
                            responsibility="Consume source output.",
                            dependency_keys=("source",),
                        ),
                    ),
                ),
                expected_project_revision=2,
                idempotency_key=f"impact:reshape:{uuid4()}",
            )
            await app.resolve_project_reshape(
                proposal_id=reshape.id,
                decision=ProjectReshapeStatus.APPLIED,
                expected_project_revision=2,
                idempotency_key=f"impact:reshape-apply:{uuid4()}",
            )
            modules = tuple(
                await store.list_modules(project.id, requirement_revision_id=requirement.id)
            )
            source, consumer = modules
            evidence = EvidenceBinding(
                project_id=project.id,
                module_id=source.id,
                claim="Replacement source provides the required output.",
                source_url="https://example.test/replacement-source",
                snapshot_hash=_hash("replacement-source-evidence"),
                span_text="Required output is documented.",
                status=EvidenceStatus.SUPPORTED,
                observed_at=datetime.now(UTC),
            )
            candidate = Candidate(
                project_id=project.id,
                module_id=source.id,
                name="Replacement source",
                description="A supported replacement source candidate.",
                evidence_binding_ids=(evidence.id,),
            )
            decision = DecisionRequest(
                project_id=project.id,
                basis_hash=_hash("impact-decision"),
                question="Use this solution?",
                options=(
                    DecisionOption(
                        option_id="approve",
                        label="Approve",
                        summary="Approve the fixture.",
                        legacy_unbound=True,
                    ),
                    DecisionOption(
                        option_id="reject",
                        label="Reject",
                        summary="Reject the fixture.",
                        legacy_unbound=True,
                    ),
                ),
                status=DecisionStatus.APPROVED,
                selected_option_id="approve",
                resolved_at=datetime.now(UTC),
            )
            solution = SolutionVersion(
                project_id=project.id,
                version=1,
                requirement_revision_id=requirement.id,
                basis_hash=_hash("impact-solution"),
                module_snapshots=(
                    {"module_id": str(source.id), "snapshot_hash": _hash("source")},
                    {"module_id": str(consumer.id), "snapshot_hash": _hash("consumer")},
                ),
                dependencies=(
                    SolutionDependency(
                        producer_module_id=source.id,
                        consumer_module_id=consumer.id,
                        kind=SolutionDependencyKind.DATA,
                        interface_key="source-to-consumer",
                        evidence_refs=("evidence-binding://fixture",),
                    ),
                ),
                dependency_projection_complete=True,
                approved_decision_id=decision.id,
            )
            await store.add_evidence_bindings((evidence,))
            await store.add_candidates((candidate,))
            await store.add_decision_request(decision)
            await store.add_solution_version(solution)
            current = await store.get_project(project.id)
            assert current is not None and current.revision == 3
            await store.update_project(
                current.model_copy(
                    update={
                        "active_solution_version_id": solution.id,
                        "revision": 4,
                        "updated_at": datetime.now(UTC),
                    }
                ),
                expected_revision=3,
            )
            observation = await app.submit_observation(
                project_id=project.id,
                expected_project_revision=4,
                statement="The source output changed.",
                affected_module_ids=(source.id,),
                idempotency_key=f"impact:observation:{uuid4()}",
            )
        api = create_app(factory, artifact_root=tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            queued = await client.post(
                f"/api/projects/{project.id}/agent-runs/impact",
                json={"observation_id": str(observation.id)},
                headers={
                    "Idempotency-Key": f"impact:run:{uuid4()}",
                    "If-Match": '"5"',
                },
            )
        assert queued.status_code == 202, queued.text
        async with factory() as session:
            run = await AgentRunControl(session).get(UUID(queued.json()["agent_run"]["id"]))
            assert run is not None

        executor = ImpactProposalGraphRunExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            checkpointer=InMemorySaver(),
            analyst=_ImpactAnalyst(
                module_id=source.id,
                candidate_id=candidate.id,
                evidence_id=evidence.id,
                snapshot=_hash("source"),
            ),
        )
        execution = await ImpactProposalLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=factory,
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING,
                            compiled_graph=executor,
                        ),
                    )
                ),
                worker_id="impact-graph-test",
            )
        ).run_once()

        assert execution is not None
        assert execution.readiness == "ready"
        assert execution.agent_decision is not None
        async with factory() as session:
            completed = await AgentRunControl(session).get(run.id)
            assert completed is not None and completed.status.value == "waiting"
            proposal = ImpactProposalManifest.model_validate(
                await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                    project_id=project.id,
                    basis_hash=run.basis_hash,
                    ref=execution.agent_decision.proposal_manifest_ref,
                    expected_kind="impact_proposal_manifest",
                )
            )
        assert proposal.impact_report_manifest_ref.startswith("artifact+sha256://")
        assert proposal.patch_candidate_ref.startswith("artifact+sha256://")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            resolved = await client.post(
                f"/api/agent-run-decisions/{execution.agent_decision.id}/resolve",
                json={"decision": "approved", "basis_hash": run.basis_hash},
                headers={
                    "Idempotency-Key": f"impact:approve:{uuid4()}",
                    "If-Match": '"5"',
                },
            )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["impact"]["status"] == "approved"
        assert resolved.json()["solution"]["previous_version_id"] == str(solution.id)
        assert resolved.headers["etag"] == '"7"'
        async with factory() as session:
            final_run = await AgentRunControl(session).get(run.id)
            final_project = await PostgresDomainStore(session).get_project(project.id)
        assert final_run is not None and final_run.status.value == "succeeded"
        assert final_project is not None and final_project.revision == 7
    finally:
        await engine.dispose()
