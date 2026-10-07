from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.api.app import create_app
from aidison.application.agent_run_application import DEFAULT_RESEARCH_RUNTIME_BINDING
from aidison.application.langgraph_worker import LangGraphOrchestrationWorker
from aidison.application.research_run_execution import ResearchLangGraphWorker, ResearchRunExecutor
from aidison.application.service import ProjectApplication, canonical_hash
from aidison.application.single_task_research import SingleTaskResearcher
from aidison.domain.models import ExecutionPlanProposal, ExecutionPlanStatus
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.model_budget_port import PostgresModelAttemptBudgetPort
from aidison.infrastructure.store import PostgresDomainStore
from aidison.providers.model_gateway import ModelGateway, ModelTarget, ProviderFailureClass
from aidison.research.coverage import CoverageContract
from aidison.research.researcher import (
    GatewayJsonModeSingleTaskResearcher,
    JsonModeResearchProviderAdapter,
)
from aidison.research.source_collection import CollectedResearchSource
from aidison.research.source_observations import SourceIdentity, SourceKind
from aidison.runtime.agent_run_events import replay_agent_run
from aidison.runtime.contracts import CoordinationMode
from aidison.runtime.graphs import GraphRegistry, RegisteredGraph

pytestmark = pytest.mark.integration


def _remove_test_artifact_bytes(*, root: Path, storage_key: str) -> Path:
    """Delete one temp Artifact payload to exercise a damaged read projection."""

    target = (root / storage_key).resolve()
    assert root.resolve() in target.parents
    target.unlink()
    return target


def _module_discovery_model(
    modules: tuple[dict[str, str], ...] = (
        {
            "key": "research",
            "name": "Research",
            "responsibility": "Produce one proposal",
        },
    ),
) -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "Research needs one bounded module.",
                    "modules": list(modules),
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


def _research_strategy_model() -> BaseChatModel:
    """Return a deterministic test double for the real strategy-only entrypoint."""

    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def reply(messages: list[dict[str, str]]) -> AIMessage:
        context = json.loads(messages[-1]["content"])
        scope = context["allowed_scope"]
        is_deep = context["research_depth"] == "deep"
        return AIMessage(
            content=json.dumps(
                {
                    "summary": "Research each approved module through the strategy contract.",
                    "scope_module_ids": [item["id"] for item in scope],
                    "tasks": [
                        {
                            "task_key": f"{item['key']}.research",
                            "title": f"Research {item['name']}",
                            "objective": f"Find evidence for {item['name']}.",
                            "module_ids": [item["id"]],
                            "depends_on_task_keys": [],
                            "priority": "must" if is_deep else "should",
                            "expected_outputs": (
                                ["evidence", "candidate", "compatibility"]
                                if item["dependencies"]
                                else ["evidence", "candidate"]
                            ),
                            "stop_conditions": ["Record evidence or an explicit gap."],
                        }
                        for item in scope
                    ],
                    "source_strategy": "primary",
                }
            )
        )

    bound.ainvoke = AsyncMock(side_effect=reply)
    model.bind.return_value = bound
    return model


def _dependent_research_strategy_model() -> BaseChatModel:
    """A strategy whose second task needs admitted findings from the first."""

    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def reply(messages: list[dict[str, str]]) -> AIMessage:
        scope = {
            item["key"]: item["id"]
            for item in json.loads(messages[-1]["content"])["allowed_scope"]
        }
        return AIMessage(
            content=json.dumps(
                {
                    "summary": "Establish power facts before checking control compatibility.",
                    "scope_module_ids": [scope["power"], scope["control"]],
                    "tasks": [
                        {
                            "task_key": "power.sources",
                            "title": "Establish power constraints",
                            "objective": "Find source-backed power constraints.",
                            "module_ids": [scope["power"]],
                            "depends_on_task_keys": [],
                            "priority": "must",
                            "expected_outputs": ["evidence", "constraint"],
                            "stop_conditions": ["Record source-backed constraints or a gap."],
                        },
                        {
                            "task_key": "control.compatibility",
                            "title": "Check control compatibility",
                            "objective": "Use upstream leads to focus compatibility retrieval.",
                            "module_ids": [scope["control"]],
                            "depends_on_task_keys": ["power.sources"],
                            "priority": "must",
                            "expected_outputs": ["evidence", "compatibility"],
                            "stop_conditions": ["Record source-backed compatibility or a gap."],
                        },
                    ],
                    "source_strategy": "mixed",
                }
            )
        )

    bound.ainvoke = AsyncMock(side_effect=reply)
    model.bind.return_value = bound
    return model


async def _propose_strategy_plan(
    client: httpx.AsyncClient,
    *,
    project_id: str,
    key: str,
    requires_independent_verification: bool = False,
) -> httpx.Response:
    return await client.post(
        f"/api/projects/{project_id}/execution-plans/research-strategy",
        # These runtime mechanics fixtures intentionally isolate their
        # one-gap/one-verifier assertions from the product default depth.
        json={
            "requires_independent_verification": requires_independent_verification,
            "research_depth": "standard",
        },
        headers={"Idempotency-Key": f"{key}:plan", "If-Match": '"3"'},
    )


class _Researcher(SingleTaskResearcher):
    def __init__(self) -> None:
        self.call_count = 0

    async def research(
        self,
        *,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object:
        self.call_count += 1
        return {
            "question": question,
            "summary": "A bounded modern runtime proposal.",
            "recommended_option": "recommended",
            "alternatives": ("alternative",),
            "evidence_claims": _evidence_claims(question, evidence_context),
        }


class _ModuleResearcher(_Researcher):
    def __init__(self) -> None:
        self.questions: list[str] = []

    async def research(
        self,
        *,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object:
        self.questions.append(question)
        module = "power" if "Module: Power" in question else "control"
        return {
            "question": question,
            "summary": f"Evidence-bound {module} proposal.",
            "recommended_option": f"{module}-recommended",
            "alternatives": (f"{module}-alternative",),
            "evidence_claims": _evidence_claims(question, evidence_context),
        }


class _GapThenEvidenceResearcher(_Researcher):
    """Leave the initial MUST key unanswered, then answer its bounded gap task."""

    def __init__(self) -> None:
        super().__init__()
        self.questions: list[str] = []

    async def research(
        self,
        *,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object:
        self.call_count += 1
        self.questions.append(question)
        return {
            "question": question,
            "summary": "A proposal that becomes evidence-complete after one gap patch.",
            "recommended_option": "recommended",
            "alternatives": ("alternative",),
            "evidence_claims": (
                () if self.call_count == 1 else _evidence_claims(question, evidence_context)
            ),
        }


class _StaticResearchSourceCollector:
    async def collect(
        self,
        *,
        run: object,
        task: object,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]:
        assert run and task and question
        return (
            CollectedResearchSource(
                key="fixture-spec",
                source=SourceIdentity(
                    kind=SourceKind.DOCUMENT,
                    provider="fixture-provider",
                    canonical_locator="fixture://research-specification",
                ),
                normalized_document="Fixture specification supports the proposed module design.",
                media_type="text/plain",
                representation="normalized-document-v1",
                parser_revision="fixture-parser-v1",
                observed_at=datetime.now(UTC),
                coverage_source_kinds=("evidence", "specification"),
            ),
        )


class _GatewayPermit:
    async def release(self) -> None:
        return None


class _GatewayQuota:
    async def acquire(self, *, bucket_key: str, deadline: datetime) -> _GatewayPermit:
        assert bucket_key and deadline
        return _GatewayPermit()


class _GatewayCircuit:
    async def allow_request(self, *, key: str, deadline: datetime) -> bool:
        assert key and deadline
        return True

    async def record_success(self, *, key: str) -> None:
        assert key

    async def record_failure(self, *, key: str, failure: ProviderFailureClass) -> None:
        assert key and failure


def _gateway_researcher(
    *,
    factory: async_sessionmaker[AsyncSession],
    artifact_root: Path,
) -> GatewayJsonModeSingleTaskResearcher:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def invoke(messages: list[dict[str, str]]) -> AIMessage:
        question = messages[1]["content"]
        return AIMessage(
            content=json.dumps(
                {
                    "question": question,
                    "summary": "A budgeted gateway research proposal.",
                    "recommended_option": "recommended",
                    "alternatives": ["alternative"],
                    "evidence_claims": _evidence_claims(
                        question,
                        (
                            CollectedResearchSource(
                                key="fixture-spec",
                                source=SourceIdentity(
                                    kind=SourceKind.DOCUMENT,
                                    provider="fixture-provider",
                                    canonical_locator="fixture://research-specification",
                                ),
                                normalized_document=(
                                    "Fixture specification supports the proposed module design."
                                ),
                                media_type="text/plain",
                                representation="normalized-document-v1",
                                parser_revision="fixture-parser-v1",
                                observed_at=datetime.now(UTC),
                                coverage_source_kinds=("evidence", "specification"),
                            ),
                        ),
                    ),
                }
            ),
            usage_metadata={"input_tokens": 20, "output_tokens": 11, "total_tokens": 31},
            response_metadata={"id": "gateway-fixture-request"},
        )

    bound.ainvoke = AsyncMock(side_effect=invoke)
    model.bind.return_value = bound
    target = ModelTarget(
        provider="fixture",
        model="fixture-json-model",
        revision="fixture-v1",
        credential_pool_id="fixture-pool",
        quota_group="research",
        capabilities=("structured_output",),
    )
    gateway = ModelGateway(
        adapter=JsonModeResearchProviderAdapter(
            model=model,
            session_factory=factory,
            artifact_root=artifact_root,
        ),
        quota=_GatewayQuota(),
        circuit=_GatewayCircuit(),
        budget=PostgresModelAttemptBudgetPort(session_factory=factory),
    )
    return GatewayJsonModeSingleTaskResearcher(
        gateway=gateway,
        session_factory=factory,
        artifact_root=artifact_root,
        target=target,
        reserved_tokens_per_call=4_000,
        max_output_tokens=1_000,
    )


def _evidence_claims(
    question: str,
    evidence_context: tuple[CollectedResearchSource, ...],
) -> tuple[dict[str, str], ...]:
    if not evidence_context:
        return ()
    keys = tuple(
        matched.group(1)
        for line in question.splitlines()
        if (matched := re.match(r"^([a-z][a-z0-9_.-]{0,159}):", line)) is not None
        and matched.group(1) != "project.objective"
    )
    return tuple(
        {
            "coverage_key": key,
            "source_key": "fixture-spec",
            "quote_text": "supports the proposed module design",
            "claim": f"The fixture specification supports {key}.",
            "subject_identity": f"fixture:{key}",
            "predicate": "supports_requirement",
            "applicability": "fixture-project",
            "normalization_schema": "fixture-support-v1",
            "normalized_value": "supported",
        }
        for key in keys
    )


@pytest.mark.asyncio
async def test_default_research_run_blocks_before_model_when_no_source_adapter_is_configured(
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
            research_strategy_model_factory=_research_strategy_model,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={
                    "name": "AgentRun application fixture",
                    "goal": "Verify new runtime authorization",
                },
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={
                    "goal": "Research one bounded module",
                },
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
            plan_response = await _propose_strategy_plan(
                client, project_id=project_id, key=key
            )
            assert plan_response.status_code == 201, plan_response.text
            plan = plan_response.json()["execution_plan"]
            resolved = await client.post(
                f"/api/execution-plans/{plan['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": plan["scope_hash"],
                    "enqueue_research": False,
                },
                headers={"Idempotency-Key": f"{key}:approve", "If-Match": '"3"'},
            )
            assert resolved.status_code == 200, resolved.text
            first = await client.post(
                f"/api/projects/{project_id}/agent-runs/research",
                json={"execution_plan_id": plan["id"]},
                headers={"Idempotency-Key": f"{key}:new-runtime", "If-Match": '"3"'},
            )
            assert first.status_code == 202, first.text
            replayed = await client.post(
                f"/api/projects/{project_id}/agent-runs/research",
                json={"execution_plan_id": plan["id"]},
                headers={"Idempotency-Key": f"{key}:new-runtime-retry", "If-Match": '"3"'},
            )
            assert replayed.status_code == 202, replayed.text
            assert first.json()["agent_run"]["id"] == replayed.json()["agent_run"]["id"]
            coverage_ref = first.json()["agent_run"]["coverage_contract_ref"]
            assert isinstance(coverage_ref, str)
            assert coverage_ref == replayed.json()["agent_run"]["coverage_contract_ref"]
        async with factory() as session:
            run = await AgentRunControl(session).get(UUID(first.json()["agent_run"]["id"]))
            assert run is not None
            assert run.coverage_contract_ref == coverage_ref
            run_events = await PostgresDomainStore(session).list_aggregate_events(
                UUID(project_id),
                aggregate_type="agent_run",
                aggregate_id=run.id,
            )
            replayed_run = replay_agent_run(list(run_events)).agent_run
            assert replayed_run == run
            account = await AgentRunBudgetLedger(session).get_account_for_run(run.id)
            assert account.token_cap == plan["max_token_budget"]
            assert account.tool_call_cap == 0
            payload = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=UUID(project_id),
                basis_hash=run.basis_hash,
                ref=coverage_ref,
                expected_kind="research_coverage_contract",
            )
            assert isinstance(payload, dict)
            assert payload["basis_hash"] == run.basis_hash
            assert CoverageContract.model_validate(payload).content_hash == payload["content_hash"]
        researcher = _Researcher()
        execution = await ResearchLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=factory,
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
                            compiled_graph=ResearchRunExecutor(
                                session_factory=factory,
                                artifact_root=tmp_path,
                                checkpointer=InMemorySaver(),
                                researcher=researcher,
                            ),
                        ),
                    )
                ),
                worker_id="modern-research-test",
            )
        ).run_once()
        assert execution is not None
        assert execution.readiness == "blocked"
        assert execution.agent_decision is None
        assert researcher.call_count == 0
        async with factory() as session:
            blocked = await AgentRunControl(session).get(run.id)
        assert blocked is not None and blocked.status.value == "failed"
        async with factory() as session:
            lifecycle_events = await PostgresDomainStore(session).list_aggregate_events(
                UUID(project_id),
                aggregate_type="agent_run",
                aggregate_id=run.id,
            )
        assert [event.aggregate_version for event in lifecycle_events] == [1, 2, 3]
        assert replay_agent_run(list(lifecycle_events)).agent_run == blocked
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            snapshot_response = await client.get(f"/api/projects/{project_id}/snapshot")
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        failed_run = next(
            item for item in snapshot["agent_runs"] if item["id"] == str(run.id)
        )
        assert failed_run["latest_error"] == (
            "没有收集到可保存、可核验的研究来源（no_trusted_research_sources）"
        )
        failed_work = snapshot["workspace"]["work"][-1]
        assert failed_work["latest_error"] == failed_run["latest_error"]
        assert failed_run["latest_error"] in snapshot["workspace"]["attention"]["reason"]
        assert "项目和组成部分事实没有被改写" in snapshot["workspace"]["attention"]["reason"]
        quality = failed_run["research_quality"]
        assert quality is not None
        assert failed_work["failure_coverage_keys"] == sorted(quality["gap_coverage_keys"])
        expected_module_ids = sorted(
            {
                module_id
                for item in quality["coverage"]
                if item["coverage_key"] in set(quality["gap_coverage_keys"])
                for module_id in item["module_ids"]
            }
        )
        assert failed_work["affected_module_ids"] == expected_module_ids
        assert snapshot["workspace"]["attention"]["affected_module_ids"] == expected_module_ids
        assert quality["gap_coverage_keys"][0] in snapshot["workspace"]["attention"]["reason"]
        async with factory() as session:
            metadata = await ContentAddressedArtifactStore(session, tmp_path).get_metadata(
                project_id=UUID(project_id),
                artifact_id=ContentAddressedArtifactStore.parse_ref(coverage_ref)[1],
            )
        await asyncio.to_thread(
            _remove_test_artifact_bytes,
            root=tmp_path,
            storage_key=metadata.storage_key,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            damaged_snapshot_response = await client.get(f"/api/projects/{project_id}/snapshot")
        assert damaged_snapshot_response.status_code == 200, damaged_snapshot_response.text
        damaged_run = next(
            item
            for item in damaged_snapshot_response.json()["agent_runs"]
            if item["id"] == str(run.id)
        )
        assert damaged_run["research_quality"] == {
            "outcome": "unavailable",
            "reason_codes": ["research_quality_projection_unavailable"],
            "gap_coverage_keys": [],
            "conflict_count": 0,
            "coverage": [],
        }
        async with factory() as session:
            unchanged = await ContentAddressedArtifactStore(session, tmp_path).get_metadata(
                project_id=UUID(project_id),
                artifact_id=metadata.id,
            )
        assert unchanged.status.value == "present"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_research_run_rejects_a_legacy_approved_plan_without_ai_strategy(
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
            research_strategy_model_factory=_research_strategy_model,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Legacy plan fixture", "goal": "Research one module"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert created.status_code == 201, created.text
            project_id = UUID(created.json()["id"])
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={"goal": "Research one module"},
                headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
            )
            assert requirements.status_code == 200, requirements.text
            discovery = await client.post(
                f"/api/projects/{project_id}/module-discovery",
                headers={"Idempotency-Key": f"{key}:discover", "If-Match": '"2"'},
            )
            assert discovery.status_code == 201, discovery.text
            applied = await client.post(
                f"/api/reshape-proposals/{discovery.json()['reshape_proposal']['id']}/resolve",
                json={"decision": "applied"},
                headers={"Idempotency-Key": f"{key}:apply", "If-Match": '"2"'},
            )
            assert applied.status_code == 200, applied.text

        async with factory() as session:
            store = PostgresDomainStore(session)
            project = await store.get_project(project_id)
            assert project is not None and project.active_requirement_revision_id is not None
            modules = await ProjectApplication(store)._active_modules(project)
            legacy = await ProjectApplication(store).propose_execution_plan(
                proposal=ExecutionPlanProposal(
                    project_id=project_id,
                    basis_hash=canonical_hash(project.active_requirement_revision_id, modules),
                    objective="Legacy fixed research plan",
                    work_summary=("legacy only",),
                    allowed_coordination_modes=(CoordinationMode.DECOMPOSE,),
                    max_concurrency=1,
                    max_token_budget=4_000,
                    allowed_tool_classes=("web_search",),
                    allowed_effects=(),
                    requires_result_approval=True,
                ),
                expected_project_revision=project.revision,
                idempotency_key=f"{key}:legacy-plan",
            )
            approved = await ProjectApplication(store).resolve_execution_plan(
                proposal_id=legacy.id,
                decision=ExecutionPlanStatus.APPROVED,
                scope_hash=legacy.scope_hash or "",
                expected_project_revision=project.revision,
                idempotency_key=f"{key}:legacy-approve",
            )
            assert approved.status is ExecutionPlanStatus.APPROVED

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            rejected = await client.post(
                f"/api/projects/{project_id}/agent-runs/research",
                json={"execution_plan_id": str(legacy.id)},
                headers={"Idempotency-Key": f"{key}:legacy-run", "If-Match": '"3"'},
            )
            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
        assert rejected.status_code == 409, rejected.text
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["agent_runs"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_module_research_run_uses_admitted_parallel_tasks_and_commits_integrated_choice(
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
            module_discovery_model_factory=lambda: _module_discovery_model(
                (
                    {
                        "key": "power",
                        "name": "Power",
                        "responsibility": "Provide stable electrical power",
                    },
                    {
                        "key": "control",
                        "name": "Control",
                        "responsibility": "Control system behavior",
                    },
                )
            ),
            research_strategy_model_factory=_research_strategy_model,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Parallel research fixture", "goal": "Research two modules"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={"goal": "Research a power and control design"},
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
            plan_response = await _propose_strategy_plan(
                client, project_id=project_id, key=key
            )
            assert plan_response.status_code == 201, plan_response.text
            plan = plan_response.json()["execution_plan"]
            approved = await client.post(
                f"/api/execution-plans/{plan['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": plan["scope_hash"],
                    "enqueue_research": False,
                },
                headers={"Idempotency-Key": f"{key}:approve", "If-Match": '"3"'},
            )
            assert approved.status_code == 200, approved.text
            queued = await client.post(
                f"/api/projects/{project_id}/agent-runs/research",
                json={"execution_plan_id": plan["id"]},
                headers={"Idempotency-Key": f"{key}:run", "If-Match": '"3"'},
            )
            assert queued.status_code == 202, queued.text
            run_id = UUID(queued.json()["agent_run"]["id"])

        researcher = _gateway_researcher(factory=factory, artifact_root=tmp_path)
        execution = await ResearchLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=factory,
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
                            compiled_graph=ResearchRunExecutor(
                                session_factory=factory,
                                artifact_root=tmp_path,
                                checkpointer=InMemorySaver(),
                                researcher=researcher,
                                source_collector=_StaticResearchSourceCollector(),
                            ),
                        ),
                    )
                ),
                worker_id="parallel-research-test",
            )
        ).run_once()
        assert execution is not None
        assert execution.readiness == "ready"
        assert execution.agent_decision is not None
        async with factory() as session:
            run = await AgentRunControl(session).get(run_id)
            assert run is not None and run.status.value == "waiting"
            account = await AgentRunBudgetLedger(session).get_account_for_run(run.id)
            assert account.token_consumed == 62
            assert account.token_reserved == 0
            context_manifests = await ContentAddressedArtifactStore(
                session, tmp_path
            ).list_metadata(
                project_id=run.project_id,
                agent_run_id=run.id,
                kind="research_context_manifest",
            )
            assert len(context_manifests) == 2
            admitted_results = await AgentResultStore(session).admitted_results(run_id=run.id)
            assert {item.context_manifest_ref for item in admitted_results} == {
                item.ref for item in context_manifests
            }
            domain_store = PostgresDomainStore(session)
            workstreams = await domain_store.list_module_workstreams(run.project_id)
            memory_items_list = []
            for workstream in workstreams:
                memory_items_list.extend(
                    await domain_store.list_module_memory_items(workstream.id)
                )
            memory_items = tuple(memory_items_list)
            assert memory_items
            assert {item.source_result_id for item in memory_items} <= {
                item.id for item in admitted_results
            }
            assert all(item.evidence_refs for item in memory_items)
            for context_manifest in context_manifests:
                value = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=context_manifest.ref,
                    expected_kind="research_context_manifest",
                )
                assert value["input_token_estimate"] > 0
                assert any(
                    item["layer"] == "untrusted" for item in value["items"]
                )
            decision = execution.agent_decision
            manifest = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=UUID(project_id),
                basis_hash=run.basis_hash,
                ref=decision.proposal_manifest_ref,
                expected_kind="research_proposal_manifest",
                )
            assert manifest["schema_version"] == "research-proposal-manifest-v2"
            assert len(manifest["task_results"]) == 2
            assert {
                item["context_manifest_ref"] for item in manifest["task_results"]
            } == {item.ref for item in context_manifests}

        # A new approved plan creates a new Run on the same basis. The first
        # task still owns the project-scoped key and remains fresh; the other
        # exact module task is satisfied by a new current-Run admission built
        # from its immutable workstream memory, so it consumes no model budget.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            reuse_plan_response = await _propose_strategy_plan(
                client, project_id=project_id, key=f"{key}:reuse"
            )
            assert reuse_plan_response.status_code == 201, reuse_plan_response.text
            reuse_plan = reuse_plan_response.json()["execution_plan"]
            reuse_approved = await client.post(
                f"/api/execution-plans/{reuse_plan['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": reuse_plan["scope_hash"],
                    "enqueue_research": False,
                },
                headers={
                    "Idempotency-Key": f"{key}:reuse:approve",
                    "If-Match": '"3"',
                },
            )
            assert reuse_approved.status_code == 200, reuse_approved.text
            reuse_queued = await client.post(
                f"/api/projects/{project_id}/agent-runs/research",
                json={"execution_plan_id": reuse_plan["id"]},
                headers={
                    "Idempotency-Key": f"{key}:reuse:run",
                    "If-Match": '"3"',
                },
            )
            assert reuse_queued.status_code == 202, reuse_queued.text
            reuse_run_id = UUID(reuse_queued.json()["agent_run"]["id"])
        reuse_execution = await ResearchLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=factory,
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
                            compiled_graph=ResearchRunExecutor(
                                session_factory=factory,
                                artifact_root=tmp_path,
                                checkpointer=InMemorySaver(),
                                researcher=researcher,
                                source_collector=_StaticResearchSourceCollector(),
                            ),
                        ),
                    )
                ),
                worker_id="workstream-memory-reuse-test",
            )
        ).run_once()
        assert reuse_execution is not None
        assert reuse_execution.readiness == "ready"
        async with factory() as session:
            reuse_run = await AgentRunControl(session).get(reuse_run_id)
            assert reuse_run is not None
            reuse_account = await AgentRunBudgetLedger(session).get_account_for_run(reuse_run.id)
            assert reuse_account.token_consumed == 31
            reuse_results = await AgentResultStore(session).admitted_results(run_id=reuse_run.id)
            assert {
                item.producer_profile_ref for item in reuse_results
            } == {
                "profile://research/1",
                "system://workstream-memory-reuse/v1",
            }
            reuse_result_ids = {
                item.id
                for item in reuse_results
                if item.producer_profile_ref == "system://workstream-memory-reuse/v1"
            }
            domain_store = PostgresDomainStore(session)
            all_memory_source_ids: set[UUID | None] = set()
            for workstream in await domain_store.list_module_workstreams(reuse_run.project_id):
                all_memory_source_ids.update(
                    item.source_result_id
                    for item in await domain_store.list_module_memory_items(workstream.id)
                )
            assert reuse_result_ids.isdisjoint(all_memory_source_ids)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            projected_run = next(
                item for item in snapshot.json()["agent_runs"] if item["id"] == str(run_id)
            )
            context_summary = projected_run["research_quality"]["context"]
            assert context_summary["manifest_count"] == 2
            assert context_summary["unreadable_manifest_count"] == 0
            assert context_summary["selected_source_count"] == 2
            assert context_summary["omitted_source_count"] == 0
            assert context_summary["omission_reason_counts"] == {}
            assert context_summary["input_token_estimate"] > 0
            resolved = await client.post(
                f"/api/agent-run-decisions/{execution.agent_decision.id}/resolve",
                json={"decision": "approved", "basis_hash": run.basis_hash},
                headers={"Idempotency-Key": f"{key}:decision", "If-Match": '"3"'},
            )
        assert resolved.status_code == 200, resolved.text
        canonical = resolved.json()["canonical_decision"]
        assert len(canonical["options"][0]["candidate_ids"]) == 2
        async with factory() as session:
            project = await PostgresDomainStore(session).get_project(UUID(project_id))
            assert project is not None and project.revision == 5
            completed = await AgentRunControl(session).get(run_id)
            assert completed is not None and completed.status.value == "succeeded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dependent_strategy_task_receives_only_admitted_upstream_leads(
    tmp_path: Path,
) -> None:
    """DAG edges must transfer bounded, non-evidence leads after Admission."""

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
            module_discovery_model_factory=lambda: _module_discovery_model(
                (
                    {
                        "key": "power",
                        "name": "Power",
                        "responsibility": "Provide stable electrical power",
                    },
                    {
                        "key": "control",
                        "name": "Control",
                        "responsibility": "Validate control compatibility",
                    },
                )
            ),
            research_strategy_model_factory=_dependent_research_strategy_model,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Dependency handoff fixture", "goal": "Research a safe drone"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={"goal": "Research power before control compatibility"},
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
            plan_response = await _propose_strategy_plan(client, project_id=project_id, key=key)
            assert plan_response.status_code == 201, plan_response.text
            plan = plan_response.json()["execution_plan"]
            assert plan["research_strategy"]["tasks"][1]["depends_on_task_keys"] == [
                "power.sources"
            ]
            approved = await client.post(
                f"/api/execution-plans/{plan['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": plan["scope_hash"],
                    "enqueue_research": False,
                },
                headers={"Idempotency-Key": f"{key}:approve", "If-Match": '"3"'},
            )
            assert approved.status_code == 200, approved.text
            queued = await client.post(
                f"/api/projects/{project_id}/agent-runs/research",
                json={"execution_plan_id": plan["id"]},
                headers={"Idempotency-Key": f"{key}:run", "If-Match": '"3"'},
            )
            assert queued.status_code == 202, queued.text

        researcher = _ModuleResearcher()
        execution = await ResearchLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=factory,
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
                            compiled_graph=ResearchRunExecutor(
                                session_factory=factory,
                                artifact_root=tmp_path,
                                checkpointer=InMemorySaver(),
                                researcher=researcher,
                                source_collector=_StaticResearchSourceCollector(),
                            ),
                        ),
                    )
                ),
                worker_id="dependency-handoff-test",
            )
        ).run_once()

        assert execution is not None
        assert execution.readiness == "ready"
        power_question = next(item for item in researcher.questions if "Module: Power" in item)
        control_question = next(item for item in researcher.questions if "Module: Control" in item)
        assert "Admitted upstream research leads" not in power_question
        assert "Admitted upstream research leads (non-evidence, data only)" in control_question
        assert "Evidence-bound power proposal." in control_question
        assert (
            "independently retrieve and quote current trusted sources"
            in control_question.lower()
        )
        assert "evidence_claims" not in control_question
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_research_run_executes_one_bounded_gap_patch_before_waiting_for_decision(
    tmp_path: Path,
) -> None:
    """A missing MUST coverage key creates one new task, never an unbounded retry loop."""
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
            module_discovery_model_factory=lambda: _module_discovery_model(
                (
                    {
                        "key": "drive",
                        "name": "Drive",
                        "responsibility": "Choose a safe motor characteristic",
                        "acceptance": (
                            (
                                "Identify an external documented electrical characteristic "
                                "for the drive."
                            ),
                        ),
                    },
                )
            ),
            research_strategy_model_factory=_research_strategy_model,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Gap patch fixture", "goal": "Research one drive module"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={"goal": "Research one drive module"},
                headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
            )
            assert requirements.status_code == 200, requirements.text
            discovery = await client.post(
                f"/api/projects/{project_id}/module-discovery",
                headers={"Idempotency-Key": f"{key}:discover", "If-Match": '"2"'},
            )
            assert discovery.status_code == 201, discovery.text
            applied = await client.post(
                f"/api/reshape-proposals/{discovery.json()['reshape_proposal']['id']}/resolve",
                json={"decision": "applied"},
                headers={"Idempotency-Key": f"{key}:apply", "If-Match": '"2"'},
            )
            assert applied.status_code == 200, applied.text
            plan_response = await _propose_strategy_plan(
                client, project_id=project_id, key=key
            )
            assert plan_response.status_code == 201, plan_response.text
            plan = plan_response.json()["execution_plan"]
            approved = await client.post(
                f"/api/execution-plans/{plan['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": plan["scope_hash"],
                    "enqueue_research": False,
                },
                headers={"Idempotency-Key": f"{key}:approve", "If-Match": '"3"'},
            )
            assert approved.status_code == 200, approved.text
            queued = await client.post(
                f"/api/projects/{project_id}/agent-runs/research",
                json={"execution_plan_id": plan["id"]},
                headers={"Idempotency-Key": f"{key}:run", "If-Match": '"3"'},
            )
            assert queued.status_code == 202, queued.text
            run_id = UUID(queued.json()["agent_run"]["id"])

        researcher = _GapThenEvidenceResearcher()
        execution = await ResearchLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=factory,
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
                            compiled_graph=ResearchRunExecutor(
                                session_factory=factory,
                                artifact_root=tmp_path,
                                checkpointer=InMemorySaver(),
                                researcher=researcher,
                                source_collector=_StaticResearchSourceCollector(),
                            ),
                        ),
                    )
                ),
                worker_id="gap-patch-test",
            )
        ).run_once()

        assert execution is not None
        assert execution.readiness == "ready"
        assert execution.agent_decision is not None
        assert researcher.call_count == 2
        assert "Prior research feedback:" in researcher.questions[1]
        assert "coverage status:" in researcher.questions[1]
        assert "admitted distinct origins:" in researcher.questions[1]
        assert "repair objective:" in researcher.questions[1]
        async with factory() as session:
            run = await AgentRunControl(session).get(run_id)
            assert run is not None and run.status.value == "waiting"
            manifest = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=UUID(project_id),
                basis_hash=run.basis_hash,
                ref=execution.agent_decision.proposal_manifest_ref,
                expected_kind="research_proposal_manifest",
            )
            assert manifest["schema_version"] == "research-proposal-manifest-v3"
            assert len(manifest["task_results"]) == 2
            patches = await ContentAddressedArtifactStore(session, tmp_path).list_metadata(
                project_id=UUID(project_id), agent_run_id=run_id, kind="research_gap_patch"
            )
            assert len(patches) == 1
            guidance = await ContentAddressedArtifactStore(session, tmp_path).list_metadata(
                project_id=UUID(project_id), agent_run_id=run_id, kind="research_gap_guidance"
            )
            assert len(guidance) == 1
            guidance_value = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=UUID(project_id),
                basis_hash=run.basis_hash,
                ref=guidance[0].ref,
                expected_kind="research_gap_guidance",
            )
            assert "drive.acceptance.01" in guidance_value["guidance_by_coverage_key"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            resolved = await client.post(
                f"/api/agent-run-decisions/{execution.agent_decision.id}/resolve",
                json={"decision": "approved", "basis_hash": run.basis_hash},
                headers={"Idempotency-Key": f"{key}:decision", "If-Match": '"3"'},
            )
        assert resolved.status_code == 200, resolved.text
        assert len(resolved.json()["canonical_decision"]["options"]) == 2
        async with factory() as session:
            completed = await AgentRunControl(session).get(run_id)
            assert completed is not None and completed.status.value == "succeeded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_authorized_independent_research_verifier_is_admitted_before_decision(
    tmp_path: Path,
) -> None:
    """The extra model call is opt-in, isolated, admitted, and retained as evidence."""

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
            module_discovery_model_factory=lambda: _module_discovery_model(
                (
                    {
                        "key": "drive",
                        "name": "Drive",
                        "responsibility": "Choose a safe motor characteristic",
                        "acceptance": (
                            "Identify an external documented electrical characteristic.",
                        ),
                    },
                )
            ),
            research_strategy_model_factory=_research_strategy_model,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Verifier fixture", "goal": "Research one drive module"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={"goal": "Research one drive module"},
                headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
            )
            assert requirements.status_code == 200, requirements.text
            discovery = await client.post(
                f"/api/projects/{project_id}/module-discovery",
                headers={"Idempotency-Key": f"{key}:discover", "If-Match": '"2"'},
            )
            assert discovery.status_code == 201, discovery.text
            applied = await client.post(
                f"/api/reshape-proposals/{discovery.json()['reshape_proposal']['id']}/resolve",
                json={"decision": "applied"},
                headers={"Idempotency-Key": f"{key}:apply", "If-Match": '"2"'},
            )
            assert applied.status_code == 200, applied.text
            plan_response = await _propose_strategy_plan(
                client,
                project_id=project_id,
                key=key,
                requires_independent_verification=True,
            )
            assert plan_response.status_code == 201, plan_response.text
            plan = plan_response.json()["execution_plan"]
            assert plan["requires_independent_verification"] is True
            approved = await client.post(
                f"/api/execution-plans/{plan['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": plan["scope_hash"],
                    "enqueue_research": False,
                },
                headers={"Idempotency-Key": f"{key}:approve", "If-Match": '"3"'},
            )
            assert approved.status_code == 200, approved.text
            queued = await client.post(
                f"/api/projects/{project_id}/agent-runs/research",
                json={"execution_plan_id": plan["id"]},
                headers={"Idempotency-Key": f"{key}:run", "If-Match": '"3"'},
            )
            assert queued.status_code == 202, queued.text
            run_id = UUID(queued.json()["agent_run"]["id"])

        researcher = _Researcher()
        execution = await ResearchLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=factory,
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
                            compiled_graph=ResearchRunExecutor(
                                session_factory=factory,
                                artifact_root=tmp_path,
                                checkpointer=InMemorySaver(),
                                researcher=researcher,
                                source_collector=_StaticResearchSourceCollector(),
                            ),
                        ),
                    )
                ),
                worker_id="independent-verifier-test",
            )
        ).run_once()

        assert execution is not None
        assert execution.readiness == "ready"
        assert execution.agent_decision is not None
        assert researcher.call_count == 2
        async with factory() as session:
            run = await AgentRunControl(session).get(run_id)
            assert run is not None and run.status.value == "waiting"
            manifest = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=UUID(project_id),
                basis_hash=run.basis_hash,
                ref=execution.agent_decision.proposal_manifest_ref,
                expected_kind="research_proposal_manifest",
            )
            assert manifest["schema_version"] == "research-proposal-manifest-v3"
            assert {row["capability"] for row in manifest["task_results"]} == {
                "research",
                "research_verifier",
            }
            routes = await ContentAddressedArtifactStore(session, tmp_path).list_metadata(
                project_id=UUID(project_id), agent_run_id=run_id, kind="research_verifier_route"
            )
            assert len(routes) == 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            resolved = await client.post(
                f"/api/agent-run-decisions/{execution.agent_decision.id}/resolve",
                json={"decision": "approved", "basis_hash": run.basis_hash},
                headers={"Idempotency-Key": f"{key}:decision", "If-Match": '"3"'},
            )
        assert resolved.status_code == 200, resolved.text
        assert len(resolved.json()["canonical_decision"]["options"]) == 2
    finally:
        await engine.dispose()
