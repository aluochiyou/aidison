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
from psycopg import AsyncConnection, sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.api.app import create_app
from aidison.application.research_consolidation import (
    ResearchConsolidationApplication,
    read_research_source_collection_diagnostics,
)
from aidison.application.research_decision_bridge import ResearchDecisionBridge
from aidison.application.single_task_research import (
    AgentRunCancelled,
    AgentRunPaused,
    ResearchSourcesUnavailable,
    SingleTaskResearcher,
    SingleTaskResearchExecutor,
)
from aidison.infrastructure.agent_run_controls import AgentRunControlRequestStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.research.consolidation import SufficiencyOutcome
from aidison.research.coverage import CoverageContract, CoverageKey, CoveragePriority
from aidison.research.evidence_diagnostics import (
    ResearchEvidenceMaterializationReport,
    ResearchSourceCollectionFailure,
    ResearchSourceCollectionReport,
    ResearchSourceUnavailableReport,
)
from aidison.research.langgraph_contracts import (
    ExecutionGrant,
    ResearchResultStatus,
    TaskEnvelope,
)
from aidison.research.single_task_graph import build_single_task_research_graph
from aidison.research.source_collection import (
    CollectedResearchSource,
    ResearchSourceCollectionError,
)
from aidison.research.source_observations import SourceIdentity, SourceKind
from aidison.runtime.agent_runs import AgentRun, AgentRunKind
from aidison.runtime.checkpointing import (
    CheckpointRuntime,
    CheckpointSettings,
    normalize_psycopg_url,
)
from aidison.runtime.control_requests import AgentRunControlRequest, ControlRequestKind
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.runtime.minimal_graph import execution_thread_config

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _run(project_id: UUID, *, basis_project_revision: int) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"single-task-run-{uuid4()}",
        basis_hash=_hash("basis"),
        basis_project_revision=basis_project_revision,
        runtime_binding=RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="research",
            graph_revision="research-v1",
            state_schema_version="research-state-v1",
            profile_binding_ref="profile://research/1",
            policy_binding_ref="policy://research/1",
        ),
        thread_id=f"single-task-run-{uuid4()}",
    )


class _FakeResearcher(SingleTaskResearcher):
    async def research(
        self,
        *,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object:
        return {
            "question": question,
            "summary": "Validated single-task research output.",
            "recommended_option": "option-a",
            "alternatives": ("option-b",),
        }


class _RecordingResearcher(_FakeResearcher):
    def __init__(self) -> None:
        self.call_count = 0
        self.steering_instructions: tuple[str, ...] = ()

    async def research(
        self,
        *,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object:
        self.call_count += 1
        self.steering_instructions = steering_instructions
        return await super().research(
            question=question,
            input_refs=input_refs,
            steering_instructions=steering_instructions,
            evidence_context=evidence_context,
        )


class _UnavailableSourceCollector:
    def __init__(self, reason_code: str) -> None:
        self._reason_code = reason_code

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]:
        del run, task, question
        raise ResearchSourceCollectionError(self._reason_code)


class _EvidenceResearcher(_FakeResearcher):
    async def research(
        self,
        *,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object:
        assert evidence_context
        return {
            "question": question,
            "summary": "The source supports the proposed option.",
            "recommended_option": "option-a",
            "alternatives": ("option-b",),
            "evidence_claims": (
                {
                    "coverage_key": "research.answer",
                    "source_key": "motor-spec",
                    "quote_text": "35A peak current at 12V",
                    "claim": "The motor has a 35A peak current at 12V.",
                    "subject_identity": "motor-a",
                    "predicate": "peak_current",
                    "applicability": "project-module",
                    "normalization_schema": "current-v1",
                    "normalized_value": "35A@12V",
                },
            ),
        }


class _UnauthorizedCoverageResearcher(_EvidenceResearcher):
    async def research(
        self,
        *,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object:
        value = await super().research(
            question=question,
            input_refs=input_refs,
            steering_instructions=steering_instructions,
            evidence_context=evidence_context,
        )
        assert isinstance(value, dict)
        claim = dict(value["evidence_claims"][0])
        claim["coverage_key"] = "research.unsanctioned_subtopic"
        return {**value, "evidence_claims": (claim,)}


class _StaticResearchSourceCollector:
    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]:
        assert run.id == task.run_id
        assert question
        return (
            CollectedResearchSource(
                key="motor-spec",
                source=SourceIdentity(
                    kind=SourceKind.WEB,
                    provider="fixture-provider",
                    canonical_locator="https://example.test/motor-a",
                ),
                normalized_document="Motor A specification: 35A peak current at 12V.",
                media_type="text/plain",
                representation="normalized-document-v1",
                parser_revision="fixture-parser-v1",
                observed_at=datetime(2026, 9, 8, 9, 0, tzinfo=UTC),
                coverage_source_kinds=("evidence", "specification"),
            ),
        )


class _PartiallyUnavailableSourceCollector(_StaticResearchSourceCollector):
    """Return one usable source plus a separate failed Coverage Key."""

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]:
        sources = await super().collect(run=run, task=task, question=question)
        return tuple(
            source.model_copy(
                update={
                    "collection_failures": (
                        ResearchSourceCollectionFailure(
                            coverage_key="research.alternate",
                            reason_code="tavily_provider_unavailable",
                        ),
                    )
                }
            )
            for source in sources
        )


def _module_discovery_model() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "One research module is sufficient.",
                    "modules": [
                        {
                            "key": "research",
                            "name": "Research",
                            "responsibility": "Produce one proposal",
                        }
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


async def _prepared_project(
    factory: async_sessionmaker[AsyncSession], key: str
) -> tuple[UUID, int]:
    api = create_app(factory, module_discovery_model_factory=_module_discovery_model)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/projects",
            json={"name": "Single-task research fixture", "goal": "One bounded research task"},
            headers={"Idempotency-Key": f"{key}:project"},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        requirements = await client.post(
            f"/api/projects/{project_id}/requirements",
            json={"goal": "One bounded research task"},
            headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
        )
        assert requirements.status_code == 200, requirements.text
        discovery = await client.post(
            f"/api/projects/{project_id}/module-discovery",
            headers={"Idempotency-Key": f"{key}:discover", "If-Match": '"2"'},
        )
        assert discovery.status_code == 201, discovery.text
        proposal_id = discovery.json()["reshape_proposal"]["id"]
        applied = await client.post(
            f"/api/reshape-proposals/{proposal_id}/resolve",
            json={"decision": "applied"},
            headers={"Idempotency-Key": f"{key}:apply", "If-Match": '"2"'},
        )
        assert applied.status_code == 200, applied.text
        return UUID(project_id), 3


@pytest.mark.asyncio
async def test_single_task_research_writes_raw_artifact_then_admits_result_and_proposal(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    schema_name = f"aidison_single_task_graph_test_{uuid4().hex}"
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
            project_id, project_revision = await _prepared_project(factory, str(uuid4()))
            run = _run(project_id, basis_project_revision=project_revision)
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="single-task-worker", lease_seconds=60)
            assert claim is not None
            await session.commit()

        task = TaskEnvelope(
            run_id=run.id,
            task_key="single-research",
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="research",
            input_refs=(),
            dependency_task_ids=(),
            coverage_keys=("research.answer",),
            allowed_tool_ids=(),
            budget_ref="budget://run/1",
            idempotency_key="single-task-1",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://run/1",
            idempotency_prefix="single-task-1",
        )
        executor = SingleTaskResearchExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            researcher=_EvidenceResearcher(),
            source_collector=_StaticResearchSourceCollector(),
        )
        graph = build_single_task_research_graph(
            checkpointer=await checkpoint_runtime.start(),
            executor=executor,
            run=run,
            claim=claim,
            task=task,
            grant=grant,
        )
        state = await graph.ainvoke(
            {
                "run_id": str(run.id),
                "question": "Which option should the project choose?",
            },
            execution_thread_config(thread_id=run.thread_id, generation=claim.generation),
        )
        assert state["raw_artifact_ref"].startswith("artifact+sha256://")
        assert state["admitted_result_ref"].startswith("admitted://agent-run-results/")
        assert state["proposal_manifest_ref"].startswith("artifact+sha256://")
        decision_bridge = ResearchDecisionBridge(session_factory=factory)
        decision, checkpoint = await decision_bridge.prepare_from_interrupt(
            run=run,
            claim=claim,
            graph=graph,
        )
        assert decision.proposal_manifest_ref == state["proposal_manifest_ref"]
        assert checkpoint.generation == claim.generation
        decision_api = create_app(factory, artifact_root=tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=decision_api), base_url="http://test"
        ) as client:
            raw_artifact_id = UUID(str(state["raw_artifact_ref"]).rsplit("/", 1)[1])
            metadata = await client.get(f"/api/projects/{project_id}/artifacts/{raw_artifact_id}")
            assert metadata.status_code == 200, metadata.text
            content = await client.get(
                f"/api/projects/{project_id}/artifacts/{raw_artifact_id}/content"
            )
            assert content.status_code == 200, content.text
            assert (
                content.headers["x-content-hash"]
                == str(state["raw_artifact_ref"])[len("artifact+sha256://") :].split("/", 1)[0]
            )
            wrong_project = await client.get(f"/api/projects/{uuid4()}/artifacts/{raw_artifact_id}")
            assert wrong_project.status_code == 404
            legacy_global = await client.get(f"/api/artifacts/{raw_artifact_id}")
            assert legacy_global.status_code == 404
            resolved = await client.post(
                f"/api/agent-run-decisions/{decision.id}/resolve",
                json={"decision": "approved", "basis_hash": run.basis_hash},
                headers={
                    "Idempotency-Key": f"decision:{uuid4()}",
                    "If-Match": f'"{project_revision}"',
                },
            )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["canonical_decision"]["selected_option_id"] == "option-1"
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
async def test_cancelled_claim_never_dispatches_a_new_research_call(tmp_path: Path) -> None:
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
            project_id, project_revision = await _prepared_project(factory, str(uuid4()))
            run = _run(project_id, basis_project_revision=project_revision)
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="single-task-worker", lease_seconds=60)
            assert claim is not None
            await control.request_cancel(run_id=run.id)
            await session.commit()

        task = TaskEnvelope(
            run_id=run.id,
            task_key="single-research",
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="research",
            input_refs=(),
            dependency_task_ids=(),
            coverage_keys=("research.answer",),
            allowed_tool_ids=(),
            budget_ref="budget://run/1",
            idempotency_key="single-task-cancelled",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://run/1",
            idempotency_prefix="single-task-cancelled",
        )
        researcher = _RecordingResearcher()
        executor = SingleTaskResearchExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            researcher=researcher,
        )
        with pytest.raises(AgentRunCancelled, match="before provider dispatch"):
            await executor.execute(
                run=run,
                claim=claim,
                task=task,
                grant=grant,
                question="Which option should the project choose?",
            )
        assert researcher.call_count == 0
        async with factory() as session:
            cancelled = await AgentRunControl(session).get(run.id)
            assert cancelled is not None
            assert cancelled.status.value == "cancelled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_collector", "expected_reason_code", "should_defer"),
    (
        (None, "no_trusted_research_sources", True),
        (
            _UnavailableSourceCollector("tavily_network_failure"),
            "tavily_network_failure",
            True,
        ),
        (
            _UnavailableSourceCollector("github_network_failure"),
            "github_network_failure",
            True,
        ),
        (
            _UnavailableSourceCollector("tavily_authentication_failed"),
            "tavily_authentication_failed",
            False,
        ),
        (
            _UnavailableSourceCollector("github_authentication_failed"),
            "github_authentication_failed",
            False,
        ),
    ),
)
async def test_empty_source_collection_records_an_admitted_partial_without_model_call(
    tmp_path: Path,
    source_collector: object | None,
    expected_reason_code: str,
    should_defer: bool,
) -> None:
    """A recoverable source gap preserves the task for Coverage/patch planning."""
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
            project_id, project_revision = await _prepared_project(factory, str(uuid4()))
            run = _run(project_id, basis_project_revision=project_revision)
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="single-task-worker", lease_seconds=60)
            assert claim is not None
            await session.commit()

        task = TaskEnvelope(
            run_id=run.id,
            task_key="single-research",
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="research",
            input_refs=(),
            dependency_task_ids=(),
            coverage_keys=("research.answer",),
            allowed_tool_ids=(),
            budget_ref="budget://run/no-sources",
            idempotency_key="single-task-no-sources",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://run/no-sources",
            idempotency_prefix="single-task-no-sources",
        )
        researcher = _RecordingResearcher()
        executor = SingleTaskResearchExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            researcher=researcher,
            source_collector=source_collector,  # type: ignore[arg-type]
        )

        if not should_defer:
            with pytest.raises(ResearchSourcesUnavailable, match=expected_reason_code):
                await executor.execute(
                    run=run,
                    claim=claim,
                    task=task,
                    grant=grant,
                    question="Which option should the project choose?",
                )
            assert researcher.call_count == 0
            return

        execution = await executor.execute(
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            question="Which option should the project choose?",
        )

        assert researcher.call_count == 0
        assert execution.raw_artifact_ref is None
        assert execution.result.status is ResearchResultStatus.PARTIAL
        assert execution.result.evidence_refs == ()
        assert len(execution.result.unresolved_refs) == 1
        async with factory() as session:
            report_value = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=execution.result.unresolved_refs[0],
                expected_kind="research_source_unavailable_report",
            )
            report = ResearchSourceUnavailableReport.model_validate(report_value)
            diagnostics = await read_research_source_collection_diagnostics(
                session=session,
                artifact_root=tmp_path,
                run=run,
            )
        assert report.result_id == execution.result.id
        assert report.task_id == task.id
        assert report.reason_code == expected_reason_code
        assert report.coverage_keys == task.coverage_keys
        assert diagnostics == {
            "research.answer": {
                "collected_source_count": 0,
                "collection_profiles": [],
                "collected_source_kinds": [],
                "unavailable_reason_codes": [expected_reason_code],
            }
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_single_task_graph_keeps_source_gap_partial_without_raw_model_artifact(
    tmp_path: Path,
) -> None:
    """A no-source PARTIAL is a valid public graph result, not a graph failure."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    schema_name = f"aidison_source_gap_graph_test_{uuid4().hex}"
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
            project_id, project_revision = await _prepared_project(factory, str(uuid4()))
            run = _run(project_id, basis_project_revision=project_revision)
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="source-gap-graph-worker", lease_seconds=60)
            assert claim is not None
            await session.commit()

        task = TaskEnvelope(
            run_id=run.id,
            task_key="source-gap-research",
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="research",
            input_refs=(),
            dependency_task_ids=(),
            coverage_keys=("research.answer",),
            allowed_tool_ids=(),
            budget_ref="budget://run/source-gap",
            idempotency_key="single-task-source-gap",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://run/source-gap",
            idempotency_prefix="single-task-source-gap",
        )
        researcher = _RecordingResearcher()
        graph = build_single_task_research_graph(
            checkpointer=await checkpoint_runtime.start(),
            executor=SingleTaskResearchExecutor(
                session_factory=factory,
                artifact_root=tmp_path,
                researcher=researcher,
            ),
            run=run,
            claim=claim,
            task=task,
            grant=grant,
        )

        state = await graph.ainvoke(
            {
                "run_id": str(run.id),
                "question": "Which option should the project choose?",
            },
            execution_thread_config(thread_id=run.thread_id, generation=claim.generation),
        )

        assert researcher.call_count == 0
        assert state["raw_artifact_ref"] is None
        assert state["admitted_result_ref"].startswith("admitted://agent-run-results/")
        assert state["proposal_manifest_ref"].startswith("artifact+sha256://")
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
async def test_single_task_research_only_exposes_quote_revalidated_collected_evidence(
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
            project_id, project_revision = await _prepared_project(factory, str(uuid4()))
            run = _run(project_id, basis_project_revision=project_revision)
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="evidence-worker", lease_seconds=60)
            assert claim is not None
            await session.commit()

        task = TaskEnvelope(
            run_id=run.id,
            task_key="evidence-research",
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="research",
            input_refs=(),
            dependency_task_ids=(),
            coverage_keys=("research.answer", "research.alternate"),
            allowed_tool_ids=(),
            budget_ref="budget://run/evidence",
            idempotency_key="single-task-evidence",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://run/evidence",
            idempotency_prefix="single-task-evidence",
        )
        execution = await SingleTaskResearchExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            researcher=_EvidenceResearcher(),
            source_collector=_PartiallyUnavailableSourceCollector(),
        ).execute(
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            question="Which motor specification supports the option?",
        )

        assert len(execution.result.evidence_refs) == 1
        assert len(execution.result.coverage_observation_refs) == 1
        assert execution.result.source_collection_report_ref is not None
        assert execution.result.evidence_refs[0].startswith("artifact+sha256://")
        assert execution.result.coverage_observation_refs[0].startswith("artifact+sha256://")
        async with factory() as session:
            source_report_value = await ContentAddressedArtifactStore(
                session, tmp_path
            ).read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=execution.result.source_collection_report_ref,
                expected_kind="research_source_collection_report",
            )
        source_report = ResearchSourceCollectionReport.model_validate(source_report_value)
        assert source_report.result_id == execution.result.id
        assert source_report.task_id == task.id
        assert source_report.coverage_keys == ("research.answer", "research.alternate")
        assert source_report.collected_source_count == 1
        assert source_report.source_snapshot_refs
        assert [
            failure.model_dump(mode="json")
            for failure in source_report.unavailable_coverage_reasons
        ] == [
            {
                "coverage_key": "research.alternate",
                "reason_code": "tavily_provider_unavailable",
            }
        ]
        async with factory() as session:
            diagnostics = await read_research_source_collection_diagnostics(
                session=session,
                artifact_root=tmp_path,
                run=run,
            )
        assert diagnostics["research.alternate"]["unavailable_reason_codes"] == [
            "tavily_provider_unavailable"
        ]
        consolidation = await ResearchConsolidationApplication(
            session_factory=factory,
            artifact_root=tmp_path,
        ).consolidate(
            run=run,
            coverage=CoverageContract(
                basis_hash=run.basis_hash,
                objective="Research motor evidence.",
                keys=(
                    CoverageKey(
                        key="research.answer",
                        question="Which source supports the motor choice?",
                        priority=CoveragePriority.MUST,
                        module_ids=(),
                        required_source_kinds=("evidence", "specification"),
                    ),
                    CoverageKey(
                        key="research.alternate",
                        question="Which alternate source is currently unavailable?",
                        priority=CoveragePriority.SHOULD,
                        module_ids=(),
                        required_source_kinds=("evidence",),
                    ),
                ),
            ),
        )
        assert consolidation.snapshot.sufficiency.outcome is SufficiencyOutcome.COMPLETE
        assert consolidation.artifact_ref.startswith("artifact+sha256://")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_single_task_research_records_rejected_claim_diagnostics(
    tmp_path: Path,
) -> None:
    """An unauthorized coverage key remains non-admitted but explainable."""
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
            project_id, project_revision = await _prepared_project(factory, str(uuid4()))
            run = _run(project_id, basis_project_revision=project_revision)
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="diagnostic-worker", lease_seconds=60)
            assert claim is not None
            await session.commit()

        task = TaskEnvelope(
            run_id=run.id,
            task_key="diagnostic-research",
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="research",
            input_refs=(),
            dependency_task_ids=(),
            coverage_keys=("research.answer",),
            allowed_tool_ids=(),
            budget_ref="budget://run/diagnostic",
            idempotency_key="single-task-diagnostic",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://run/diagnostic",
            idempotency_prefix="single-task-diagnostic",
        )
        execution = await SingleTaskResearchExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            researcher=_UnauthorizedCoverageResearcher(),
            source_collector=_StaticResearchSourceCollector(),
        ).execute(
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            question="Which motor specification supports the option?",
        )

        assert execution.result.status is ResearchResultStatus.PARTIAL
        assert execution.result.evidence_refs == ()
        assert execution.result.coverage_observation_refs == ()
        assert len(execution.result.unresolved_refs) == 1
        async with factory() as session:
            payload = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=execution.result.unresolved_refs[0],
                expected_kind="research_evidence_materialization_report",
            )
        report = ResearchEvidenceMaterializationReport.model_validate(payload)
        assert report.result_id == execution.result.id
        assert report.task_id == task.id
        assert report.issues[0].coverage_key == "research.unsanctioned_subtopic"
        assert report.issues[0].assigned_coverage_keys == ("research.answer",)
        assert report.issues[0].reason_codes == ("coverage_key_not_assigned_to_task",)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pause_at_pre_dispatch_safe_point_stops_research_and_runtime_steering_is_applied(
    tmp_path: Path,
) -> None:
    """A pause wins before provider dispatch; a steering instruction reaches the next call."""
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
            project_id, project_revision = await _prepared_project(factory, str(uuid4()))
            run = _run(project_id, basis_project_revision=project_revision)
            control = AgentRunControl(session)
            await control.create(run)
            await session.commit()
            claim = await control.claim_next(worker_id="single-task-worker", lease_seconds=60)
            assert claim is not None
            pause = await AgentRunControlRequestStore(session).request(
                AgentRunControlRequest(
                    agent_run_id=run.id,
                    kind=ControlRequestKind.PAUSE,
                    basis_hash=run.basis_hash,
                    idempotency_key=f"pause:{uuid4()}",
                )
            )
            await session.commit()

        task = TaskEnvelope(
            run_id=run.id,
            task_key="single-research",
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="research",
            input_refs=(),
            dependency_task_ids=(),
            coverage_keys=("research.answer",),
            allowed_tool_ids=(),
            budget_ref="budget://run/pause",
            idempotency_key="single-task-paused",
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref="deadline://run/pause",
            idempotency_prefix="single-task-paused",
        )
        researcher = _RecordingResearcher()
        executor = SingleTaskResearchExecutor(
            session_factory=factory,
            artifact_root=tmp_path,
            researcher=researcher,
            source_collector=_StaticResearchSourceCollector(),
        )
        with pytest.raises(AgentRunPaused, match="before provider dispatch"):
            await executor.execute(
                run=run,
                claim=claim,
                task=task,
                grant=grant,
                question="Which option should the project choose?",
            )
        assert researcher.call_count == 0
        async with factory() as session:
            paused_run = await AgentRunControl(session).get(run.id)
            assert paused_run is not None and paused_run.status.value == "waiting"
            [persisted_pause] = await AgentRunControlRequestStore(session).list_for_run(
                agent_run_id=run.id
            )
            assert persisted_pause.id == pause.id
            assert persisted_pause.status.value == "acknowledged"

            resumed = await AgentRunControl(session).resume_after_pause(run_id=run.id)
            resumed_claim = await AgentRunControl(session).claim_next(
                worker_id="single-task-worker", lease_seconds=60
            )
            assert resumed.status.value == "queued"
            assert resumed_claim is not None
            steering = await AgentRunControlRequestStore(session).request(
                AgentRunControlRequest(
                    agent_run_id=run.id,
                    kind=ControlRequestKind.RUNTIME_STEERING,
                    basis_hash=run.basis_hash,
                    payload={"instruction": "Prioritize independently verified evidence."},
                    idempotency_key=f"steer:{uuid4()}",
                )
            )
            await session.commit()

        resumed_grant = grant.model_copy(
            update={
                "attempt_id": uuid4(),
                "generation": resumed_claim.generation,
                "lease_token": resumed_claim.lease_token,
            }
        )
        await executor.execute(
            run=run,
            claim=resumed_claim,
            task=task,
            grant=resumed_grant,
            question="Which option should the project choose?",
        )
        assert researcher.call_count == 1
        assert researcher.steering_instructions == ("Prioritize independently verified evidence.",)
        async with factory() as session:
            requests = await AgentRunControlRequestStore(session).list_for_run(agent_run_id=run.id)
            persisted_steering = next(item for item in requests if item.id == steering.id)
            assert persisted_steering.status.value == "acknowledged"
            later_steering = await AgentRunControlRequestStore(session).request(
                AgentRunControlRequest(
                    agent_run_id=run.id,
                    kind=ControlRequestKind.RUNTIME_STEERING,
                    basis_hash=run.basis_hash,
                    payload={"instruction": "Also compare operating temperature limits."},
                    idempotency_key=f"steer-later:{uuid4()}",
                )
            )
            await session.commit()

        rebuilt_steering = await executor.consume_pre_dispatch_controls(
            run=run,
            claim=resumed_claim,
        )
        assert rebuilt_steering == (
            "Prioritize independently verified evidence.",
            "Also compare operating temperature limits.",
        )
        async with factory() as session:
            persisted_later_steering = next(
                item
                for item in await AgentRunControlRequestStore(session).list_for_run(
                    agent_run_id=run.id
                )
                if item.id == later_steering.id
            )
            assert persisted_later_steering.status.value == "acknowledged"
    finally:
        await engine.dispose()
