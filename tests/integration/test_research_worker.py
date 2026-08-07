from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from sqlalchemy import func, select, text

from aidison.application.research import AgentRunner, ResearchWorker
from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    BomItem,
    Candidate,
    CompatibilityFinding,
    DecisionOption,
    EvidenceBinding,
    ModuleSelection,
    SolutionPlanStep,
)
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.orm import (
    ArtifactRow,
    AttemptResultRow,
    AttemptRow,
    BudgetAllocationRow,
    BudgetOperationRow,
    CandidateRow,
    DecisionRequestRow,
    DelegationRow,
    EvidenceBindingRow,
    ImpactAnalysisRow,
    JobRow,
    JoinGroupRow,
    JoinReceiptRow,
    PlanTaskRow,
    ProjectRow,
    SolutionProposalRow,
)
from aidison.infrastructure.planning import PostgresPlanStore
from aidison.infrastructure.runtime import PostgresRuntime, RuntimeConflictError
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import JobClaim, JobStatus
from aidison.tools.github import ControlledGitHubRead, GitHubMcpSession
from aidison.tools.web_search import (
    ControlledWebSearch,
    RawSearchHit,
    SearchContext,
)

pytestmark = pytest.mark.integration


class FakeSearchBackend:
    async def search(self, query: str, *, max_results: int) -> tuple[RawSearchHit, ...]:
        del query
        return (
            RawSearchHit(
                title="Fixture engineering source",
                url="https://example.com/fixture",
                snippet="Fixture evidence",
            ),
        )[:max_results]


class FakePageFetcher:
    async def fetch(self, url: str) -> tuple[str, bytes]:
        assert url == "https://example.com/fixture"
        return "text/plain", b"A fixture source supports this engineering candidate."


class FakeResearchAgent:
    def __init__(self, search: ControlledWebSearch, context: SearchContext) -> None:
        self._search = search
        self._context = context

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert config is not None and len(config["callbacks"]) == 1
        prompt = str(input["messages"][0]["content"])
        callback = config["callbacks"][0]
        model_run_id = uuid4()
        await callback.on_chat_model_start(
            {"name": "fake-model"},
            [[HumanMessage(content=prompt)]],
            run_id=model_run_id,
        )
        matched = re.search(r"^- ([a-z][a-z0-9_-]+):", prompt, flags=re.MULTILINE)
        assert matched is not None
        module_key = matched.group(1)
        hit = (
            await self._search.search(
                f"{module_key} engineering candidate",
                max_results=1,
                context=self._context,
            )
        )[0]
        await callback.on_llm_end(
            LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            message=AIMessage(
                                content="typed proposal",
                                usage_metadata={
                                    "input_tokens": 100,
                                    "output_tokens": 50,
                                    "total_tokens": 150,
                                },
                            )
                        )
                    ]
                ]
            ),
            run_id=model_run_id,
        )
        return {
            "structured_response": {
                "evidence": [
                    {
                        "module_key": module_key,
                        "claim": f"The source supports {module_key}",
                        "source_url": hit.url,
                        "snapshot_hash": hit.snapshot_hash,
                        "span_text": hit.span_text,
                        "status": "supported",
                    }
                ],
                "candidates": [
                    {
                        "module_key": module_key,
                        "name": f"{module_key} candidate",
                        "description": "A fixture candidate backed by a snapshot",
                        "evidence_indexes": [0],
                        "risks": ["Needs physical verification"],
                    }
                ],
                "findings": [
                    {
                        "module_keys": [module_key],
                        "rule_id": f"{module_key}.fit",
                        "status": "needs_test",
                        "summary": "Fit requires a bench test",
                        "evidence_indexes": [0],
                        "required_test": "Measure and verify the interface",
                    }
                ],
                "decision_question": f"Choose the {module_key} candidate?",
                "decision_options": [
                    {
                        "option_id": "use-candidate",
                        "label": f"Use {module_key} candidate",
                        "summary": "Select the evidence-backed candidate for this module.",
                        "candidate_indexes": [0],
                        "evidence_indexes": [0],
                    },
                    {
                        "option_id": "compare-candidate",
                        "label": f"Compare {module_key} candidate",
                        "summary": "Keep this candidate as the evidence-backed comparison basis.",
                        "candidate_indexes": [0],
                        "evidence_indexes": [0],
                    },
                ],
            }
        }


def fake_agent_factory(
    model: BaseChatModel,
    search: ControlledWebSearch,
    context: SearchContext,
    github: ControlledGitHubRead | None,
    system_prompt: str,
) -> AgentRunner:
    del model, github
    assert system_prompt
    return FakeResearchAgent(search, context)


class FakeGitHubBackend:
    @asynccontextmanager
    async def session(self):
        yield GitHubMcpSession({})


class FakeSolutionAgent:
    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert config is not None and len(config["callbacks"]) == 1
        prompt = str(input["messages"][0]["content"])
        payload = json.loads(prompt[prompt.index("{") :])
        callback = config["callbacks"][0]
        run_id = uuid4()
        await callback.on_chat_model_start(
            {"name": "fake-solution-model"},
            [[HumanMessage(content=prompt)]],
            run_id=run_id,
        )
        await callback.on_llm_end(
            LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            message=AIMessage(
                                content="typed solution proposal",
                                usage_metadata={
                                    "input_tokens": 120,
                                    "output_tokens": 80,
                                    "total_tokens": 200,
                                },
                            )
                        )
                    ]
                ]
            ),
            run_id=run_id,
        )
        candidates_by_module = {item["module_id"]: item for item in payload["candidates"]}
        modules = payload["modules"]
        evidence_ids = [item["id"] for item in payload["evidence"]]
        finding_ids = [item["id"] for item in payload["compatibility_findings"]]
        return {
            "structured_response": {
                "module_selections": [
                    {
                        "module_key": module["key"],
                        "candidate_id": candidates_by_module[module["id"]]["id"],
                        "candidate_name": candidates_by_module[module["id"]]["name"],
                        "rationale": "Use the accepted evidence-backed candidate.",
                        "evidence_binding_ids": candidates_by_module[module["id"]][
                            "evidence_binding_ids"
                        ],
                        "risks": candidates_by_module[module["id"]]["risks"],
                    }
                    for module in modules
                ],
                "evidence_binding_ids": evidence_ids,
                "compatibility_finding_ids": finding_ids,
                "bom": [
                    {
                        "line_id": f"{module['key']}-item",
                        "module_key": module["key"],
                        "candidate_id": candidates_by_module[module["id"]]["id"],
                        "name": candidates_by_module[module["id"]]["name"],
                        "quantity": 1,
                        "unit": "piece",
                        "evidence_binding_ids": candidates_by_module[module["id"]][
                            "evidence_binding_ids"
                        ],
                    }
                    for module in modules
                ],
                "implementation_steps": [
                    {
                        "step_id": "assemble-system",
                        "title": "Assemble the system",
                        "instruction": "Assemble each selected module in dependency order.",
                        "module_keys": [item["key"] for item in modules],
                    }
                ],
                "verification_steps": [
                    {
                        "step_id": "bench-verify",
                        "title": "Bench verify the interfaces",
                        "instruction": "Run every required compatibility test.",
                        "module_keys": [item["key"] for item in modules],
                        "acceptance": ["All required interface tests pass"],
                    }
                ],
                "risks": ["Physical fit still requires a bench test"],
                "unknowns": ["Final measured tolerances"],
                "consequences": ["The build remains repairable"],
            }
        }


def fake_solution_agent_factory(model: BaseChatModel) -> AgentRunner:
    del model
    return FakeSolutionAgent()


class FakeImpactAgent:
    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert config is not None and len(config["callbacks"]) == 1
        prompt = str(input["messages"][0]["content"])
        payload = json.loads(prompt[prompt.index("{") :])
        callback = config["callbacks"][0]
        run_id = uuid4()
        await callback.on_chat_model_start(
            {"name": "fake-impact-model"},
            [[HumanMessage(content=prompt)]],
            run_id=run_id,
        )
        await callback.on_llm_end(
            LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            message=AIMessage(
                                content="typed impact proposal",
                                usage_metadata={
                                    "input_tokens": 110,
                                    "output_tokens": 70,
                                    "total_tokens": 180,
                                },
                            )
                        )
                    ]
                ]
            ),
            run_id=run_id,
        )
        affected_ids = set(payload["affected_module_ids"])
        module = next(item for item in payload["modules"] if item["id"] in affected_ids)
        base_snapshot = next(
            item
            for item in payload["base_solution"]["module_snapshots"]
            if item["module_id"] == module["id"]
        )
        candidate = next(
            item
            for item in payload["candidates"]
            if item["module_id"] == module["id"] and item["id"] != base_snapshot["candidate_id"]
        )
        return {
            "structured_response": {
                "summary": "Replace the directly affected module and bench verify it.",
                "stale_evidence_binding_ids": [],
                "module_patches": [
                    {
                        "module_key": module["key"],
                        "base_snapshot_hash": base_snapshot["snapshot_hash"],
                        "candidate_id": candidate["id"],
                        "candidate_name": candidate["name"],
                        "rationale": "The alternate candidate addresses the observation.",
                        "evidence_binding_ids": candidate["evidence_binding_ids"],
                        "risks": candidate["risks"],
                    }
                ],
                "replacement_bom_items": [
                    {
                        "line_id": f"{module['key']}-replacement",
                        "module_key": module["key"],
                        "candidate_id": candidate["id"],
                        "name": candidate["name"],
                        "quantity": 1,
                        "unit": "piece",
                        "evidence_binding_ids": candidate["evidence_binding_ids"],
                    }
                ],
                "replacement_implementation_steps": [
                    {
                        "step_id": "replace-affected-module",
                        "title": "Replace the affected module",
                        "instruction": "Install the accepted alternate candidate.",
                        "module_keys": [module["key"]],
                    }
                ],
                "replacement_verification_steps": [
                    {
                        "step_id": "verify-replacement",
                        "title": "Bench verify the replacement",
                        "instruction": "Repeat the observation under controlled conditions.",
                        "module_keys": [module["key"]],
                        "acceptance": ["The observed failure no longer reproduces"],
                    }
                ],
                "risks": ["Physical verification remains required"],
            }
        }


def fake_impact_agent_factory(model: BaseChatModel) -> AgentRunner:
    del model
    return FakeImpactAgent()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_count", (2, 8))
async def test_worker_runs_bounded_nway_research_into_one_canonical_decision(
    tmp_path: Path,
    child_count: int,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE jobs, artifacts CASCADE"))
            await session.commit()
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="Worker fixture",
                goal="Research a generic DIY system",
                idempotency_key=f"worker-project-{uuid4()}",
            )
            requirement, modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=project.revision,
                goal=project.goal,
                hard_constraints=("Use evidence",),
                preferences=("Keep it repairable",),
                available_resources=("Workshop",),
                unknowns=("Exact interfaces",),
                modules=tuple(
                    {
                        "key": f"module-{index + 1}",
                        "name": f"Module {index + 1}",
                        "responsibility": f"Own bounded responsibility {index + 1}",
                    }
                    for index in range(child_count)
                ),
                idempotency_key=f"worker-requirements-{uuid4()}",
            )
            basis_hash = sha256(
                f"{requirement.id}:{','.join(str(item.id) for item in modules)}".encode()
            ).hexdigest()
            root_job_id = await PostgresRuntime(session).create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision + 1,
                profile_id="research-orchestrator",
                profile_revision=1,
                token_budget_cap=child_count * 4_000,
                tool_call_budget_cap=child_count * 3,
            )

        worker = ResearchWorker(
            session_factory=factory,
            artifact_root=tmp_path,
            model_factory=lambda: MagicMock(spec=BaseChatModel),
            search_backend_factory=FakeSearchBackend,
            github_backend_factory=FakeGitHubBackend,
            page_fetcher=FakePageFetcher(),
            agent_factory=fake_agent_factory,
            lease_seconds=10,
            poll_seconds=0.02,
        )
        worker_task = asyncio.create_task(
            worker.run_forever(
                worker_id="integration-worker",
                concurrency=child_count + 1,
            )
        )
        try:
            async with asyncio.timeout(15):
                while True:
                    async with factory() as session:
                        root = await session.get(JobRow, root_job_id)
                        if root is not None and root.status in {"succeeded", "failed"}:
                            break
                    await asyncio.sleep(0.05)
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

        async with factory() as session:
            root = await session.get(JobRow, root_job_id)
            assert root is not None and root.status == "succeeded"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(JobRow)
                    .where(JobRow.parent_job_id == root_job_id, JobRow.status == "succeeded")
                )
                == child_count
            )
            assert (
                await session.scalar(select(func.count()).select_from(DelegationRow))
                == child_count
            )
            plan_tasks = list(
                await session.scalars(
                    select(PlanTaskRow).order_by(PlanTaskRow.logical_key)
                )
            )
            assert [item.logical_key for item in plan_tasks] == [
                f"research.shard-{index + 1}" for index in range(child_count)
            ]
            assert all(item.dispatched_job_id is not None for item in plan_tasks)
            assert {item.status for item in plan_tasks} == {"succeeded"}
            for task in plan_tasks:
                assert task.dispatched_job_id is not None
                await PostgresPlanStore(session).bind_task_job(
                    root_job_id=root_job_id,
                    logical_key=task.logical_key,
                    dispatched_job_id=task.dispatched_job_id,
                )
            assert await session.scalar(select(func.count()).select_from(JoinReceiptRow)) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(DecisionRequestRow)
                    .where(DecisionRequestRow.project_id == project.id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EvidenceBindingRow)
                    .where(EvidenceBindingRow.project_id == project.id)
                )
                == child_count
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CandidateRow)
                    .where(CandidateRow.project_id == project.id)
                )
                == child_count
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ArtifactRow)
                    .where(ArtifactRow.project_id == project.id)
                )
                == child_count * 2 + 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetOperationRow)
                    .where(BudgetOperationRow.state == "settled")
                )
                == child_count * 2
            )
            assert set(
                await session.scalars(
                    select(BudgetOperationRow.provider).where(
                        BudgetOperationRow.kind == "tool"
                    )
                )
            ) == {"tavily"}
            assert sorted(
                await session.scalars(
                    select(BudgetOperationRow.consumed_tokens).where(
                        BudgetOperationRow.kind == "model"
                    )
                )
            ) == [150] * child_count
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetAllocationRow)
                    .where(BudgetAllocationRow.status == "closed")
                )
                == child_count
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_solution_job_creates_server_owned_typed_proposal(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE jobs, artifacts CASCADE"))
            await session.commit()
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="Solution worker fixture",
                goal="Create a typed generic DIY solution",
                idempotency_key=f"solution-worker-project-{uuid4()}",
            )
            requirement, modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=1,
                goal=project.goal,
                hard_constraints=("Use evidence",),
                preferences=("Repairable",),
                available_resources=("Workshop",),
                unknowns=("Exact interfaces",),
                modules=(
                    {"key": "frame", "name": "Frame", "responsibility": "Carry the system"},
                    {
                        "key": "power",
                        "name": "Power",
                        "responsibility": "Supply safe power",
                        "dependency_keys": ("frame",),
                    },
                ),
                idempotency_key=f"solution-worker-requirements-{uuid4()}",
            )
            evidence = tuple(
                EvidenceBinding(
                    project_id=project.id,
                    module_id=module.id,
                    claim=f"{module.key} has a documented interface",
                    source_url=f"https://example.com/{module.key}",
                    snapshot_hash=sha256(module.key.encode()).hexdigest(),
                    span_text="Documented fixture interface",
                    status="supported",
                    observed_at=datetime.now(UTC),
                )
                for module in modules
            )
            candidates = tuple(
                Candidate(
                    project_id=project.id,
                    module_id=module.id,
                    name=f"{module.key} candidate",
                    description="An evidence-backed fixture candidate",
                    evidence_binding_ids=(evidence[index].id,),
                    risks=("Bench verification required",),
                )
                for index, module in enumerate(modules)
            )
            findings = tuple(
                CompatibilityFinding(
                    project_id=project.id,
                    module_ids=(module.id,),
                    rule_id=f"{module.key}.fit",
                    status="needs_test",
                    summary="Physical fit requires a bench check",
                    evidence_binding_ids=(evidence[index].id,),
                    required_test="Measure the documented interface",
                )
                for index, module in enumerate(modules)
            )
            decision = await app.submit_research_proposal(
                project_id=project.id,
                expected_project_revision=2,
                evidence=evidence,
                candidates=candidates,
                findings=findings,
                decision_question="Use the evidence-backed route?",
                decision_options=(
                    DecisionOption(
                        option_id="approve",
                        label="Use evidence-backed route",
                        summary="Freeze the current module candidates as the selected route.",
                        candidate_ids=tuple(item.id for item in candidates),
                        evidence_binding_ids=tuple(item.id for item in evidence),
                    ),
                    DecisionOption(
                        option_id="compare",
                        label="Compare the same researched basis",
                        summary="Retain the current candidates as a comparison baseline.",
                        candidate_ids=tuple(item.id for item in candidates),
                        evidence_binding_ids=tuple(item.id for item in evidence),
                    ),
                ),
                idempotency_key=f"solution-worker-research-{uuid4()}",
            )
            decision = await app.resolve_decision(
                decision_id=decision.id,
                expected_project_revision=3,
                selected_option_id="approve",
                basis_hash=decision.basis_hash,
                idempotency_key=f"solution-worker-decision-{uuid4()}",
            )
            root_job_id = await PostgresRuntime(session).create_job(
                project_id=project.id,
                kind="solution_wave",
                basis_hash=sha256(decision.model_dump_json().encode()).hexdigest(),
                basis_project_revision=4,
                profile_id="solution-orchestrator",
                profile_revision=1,
                idempotency_key=f"solution-run:{decision.id}",
                request_payload={"decision_id": str(decision.id)},
                token_budget_cap=16_000,
                tool_call_budget_cap=0,
            )

        worker = ResearchWorker(
            session_factory=factory,
            artifact_root=tmp_path,
            model_factory=lambda: MagicMock(spec=BaseChatModel),
            search_backend_factory=FakeSearchBackend,
            github_backend_factory=FakeGitHubBackend,
            page_fetcher=FakePageFetcher(),
            agent_factory=fake_agent_factory,
            solution_agent_factory=fake_solution_agent_factory,
            lease_seconds=10,
            poll_seconds=0.02,
        )
        worker_task = asyncio.create_task(
            worker.run_forever(worker_id="solution-integration-worker", concurrency=3)
        )
        try:
            async with asyncio.timeout(15):
                while True:
                    async with factory() as session:
                        root = await session.get(JobRow, root_job_id)
                        if root is not None and root.status in {"succeeded", "failed"}:
                            break
                    await asyncio.sleep(0.05)
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

        async with factory() as session:
            root = await session.get(JobRow, root_job_id)
            assert root is not None and root.status == "succeeded"
            proposal_row = await session.scalar(
                select(SolutionProposalRow).where(SolutionProposalRow.project_id == project.id)
            )
            assert proposal_row is not None and proposal_row.status == "proposed"
            proposal = await PostgresDomainStore(session).get_solution_proposal(proposal_row.id)
            assert proposal is not None
            assert {item.module_id for item in proposal.module_selections} == {
                item.id for item in modules
            }
            assert len(proposal.bom) == 2
            assert proposal.unknowns == ("Final measured tolerances",)
            assert await session.scalar(select(func.count()).select_from(JoinReceiptRow)) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetOperationRow)
                    .where(BudgetOperationRow.state == "settled")
                )
                == 1
            )
            assert await session.scalar(
                select(BudgetOperationRow.consumed_tokens).where(
                    BudgetOperationRow.kind == "model"
                )
            ) == 200
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_impact_job_creates_server_owned_typed_analysis(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE jobs, artifacts CASCADE"))
            await session.commit()
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="Impact worker fixture",
                goal="Revise only the affected DIY modules",
                idempotency_key=f"impact-worker-project-{uuid4()}",
            )
            _, modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=1,
                goal=project.goal,
                hard_constraints=("Preserve unaffected work",),
                preferences=("Repairable",),
                available_resources=("Workshop",),
                unknowns=("Measured fit",),
                modules=(
                    {"key": "frame", "name": "Frame", "responsibility": "Carry the system"},
                    {
                        "key": "power",
                        "name": "Power",
                        "responsibility": "Supply safe power",
                        "dependency_keys": ("frame",),
                    },
                ),
                idempotency_key=f"impact-worker-requirements-{uuid4()}",
            )
            evidence = EvidenceBinding(
                project_id=project.id,
                module_id=modules[0].id,
                claim="Both frame candidates expose documented mounting patterns.",
                source_url="https://example.com/frame",
                snapshot_hash=sha256(b"frame-impact-fixture").hexdigest(),
                span_text="Two documented mounting patterns are available.",
                status="supported",
                observed_at=datetime.now(UTC),
            )
            frame_base = Candidate(
                project_id=project.id,
                module_id=modules[0].id,
                name="frame base",
                description="The initially accepted frame",
                evidence_binding_ids=(evidence.id,),
            )
            frame_alternate = Candidate(
                project_id=project.id,
                module_id=modules[0].id,
                name="frame alternate",
                description="An alternate documented frame",
                evidence_binding_ids=(evidence.id,),
                risks=("Bench fit remains required",),
            )
            power = Candidate(
                project_id=project.id,
                module_id=modules[1].id,
                name="power base",
                description="The accepted power module",
            )
            finding = CompatibilityFinding(
                project_id=project.id,
                module_ids=tuple(item.id for item in modules),
                rule_id="fixture.interface",
                status="compatible",
                summary="The documented interfaces are compatible.",
                evidence_binding_ids=(evidence.id,),
            )
            decision = await app.submit_research_proposal(
                project_id=project.id,
                expected_project_revision=2,
                evidence=(evidence,),
                candidates=(frame_base, frame_alternate, power),
                findings=(finding,),
                decision_question="Use the initial route?",
                decision_options=(
                    DecisionOption(
                        option_id="approve",
                        label="Use initial route",
                        summary="Select the base frame and power candidates.",
                        candidate_ids=(frame_base.id, power.id),
                        evidence_binding_ids=(evidence.id,),
                    ),
                    DecisionOption(
                        option_id="alternate",
                        label="Use alternate frame",
                        summary="Select the alternate frame with the same power candidate.",
                        candidate_ids=(frame_alternate.id, power.id),
                        evidence_binding_ids=(evidence.id,),
                    ),
                ),
                idempotency_key=f"impact-worker-research-{uuid4()}",
            )
            decision = await app.resolve_decision(
                decision_id=decision.id,
                expected_project_revision=3,
                selected_option_id="approve",
                basis_hash=decision.basis_hash,
                idempotency_key=f"impact-worker-decision-{uuid4()}",
            )
            proposal = await app.submit_solution_proposal(
                project_id=project.id,
                expected_project_revision=4,
                decision_id=decision.id,
                module_selections=(
                    ModuleSelection(
                        module_id=modules[0].id,
                        candidate_id=frame_base.id,
                        candidate_name=frame_base.name,
                        rationale="Use the initial documented frame.",
                        evidence_binding_ids=(evidence.id,),
                    ),
                    ModuleSelection(
                        module_id=modules[1].id,
                        candidate_id=power.id,
                        candidate_name=power.name,
                        rationale="Use the accepted power module.",
                    ),
                ),
                evidence_binding_ids=(evidence.id,),
                compatibility_finding_ids=(finding.id,),
                bom=(
                    BomItem(
                        line_id="frame-base",
                        module_id=modules[0].id,
                        candidate_id=frame_base.id,
                        name=frame_base.name,
                        quantity=1,
                        unit="piece",
                        evidence_binding_ids=(evidence.id,),
                    ),
                ),
                implementation_steps=(
                    SolutionPlanStep(
                        step_id="assemble-base",
                        title="Assemble base solution",
                        instruction="Assemble both accepted modules.",
                        module_ids=tuple(item.id for item in modules),
                    ),
                ),
                verification_steps=(
                    SolutionPlanStep(
                        step_id="verify-base",
                        title="Bench verify base solution",
                        instruction="Verify the documented interfaces.",
                        module_ids=tuple(item.id for item in modules),
                    ),
                ),
                risks=(),
                unknowns=(),
                consequences=("The build remains repairable.",),
                artifact_ref="artifact://impact-worker-solution",
                profile_id="solution-proposer-ro",
                profile_revision=1,
                idempotency_key=f"impact-worker-solution-proposal-{uuid4()}",
            )
            solution = await app.freeze_solution(
                project_id=project.id,
                expected_project_revision=5,
                solution_proposal_id=proposal.id,
                basis_hash=proposal.basis_hash,
                idempotency_key=f"impact-worker-solution-{uuid4()}",
            )
            observation = await app.submit_observation(
                project_id=project.id,
                expected_project_revision=6,
                statement="The initial frame flexes under the measured load.",
                affected_module_ids=(modules[0].id,),
                idempotency_key=f"impact-worker-observation-{uuid4()}",
            )
            root_job_id = await PostgresRuntime(session).create_job(
                project_id=project.id,
                kind="impact_wave",
                basis_hash=sha256(observation.model_dump_json().encode()).hexdigest(),
                basis_project_revision=7,
                profile_id="impact-orchestrator",
                profile_revision=1,
                idempotency_key=f"impact-run:{observation.id}",
                request_payload={"observation_id": str(observation.id)},
                token_budget_cap=16_000,
                tool_call_budget_cap=0,
            )

        worker = ResearchWorker(
            session_factory=factory,
            artifact_root=tmp_path,
            model_factory=lambda: MagicMock(spec=BaseChatModel),
            search_backend_factory=FakeSearchBackend,
            github_backend_factory=FakeGitHubBackend,
            page_fetcher=FakePageFetcher(),
            agent_factory=fake_agent_factory,
            solution_agent_factory=fake_solution_agent_factory,
            impact_agent_factory=fake_impact_agent_factory,
            lease_seconds=10,
            poll_seconds=0.02,
        )
        worker_task = asyncio.create_task(
            worker.run_forever(worker_id="impact-integration-worker", concurrency=3)
        )
        try:
            async with asyncio.timeout(15):
                while True:
                    async with factory() as session:
                        root = await session.get(JobRow, root_job_id)
                        if root is not None and root.status in {"succeeded", "failed"}:
                            break
                    await asyncio.sleep(0.05)
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

        async with factory() as session:
            root = await session.get(JobRow, root_job_id)
            assert root is not None and root.status == "succeeded"
            impact_row = await session.scalar(
                select(ImpactAnalysisRow).where(ImpactAnalysisRow.project_id == project.id)
            )
            assert impact_row is not None and impact_row.status == "proposed"
            impact = await PostgresDomainStore(session).get_impact_analysis(impact_row.id)
            assert impact is not None
            assert impact.direct_affected_module_ids == (modules[0].id,)
            assert impact.transitive_affected_module_ids == (modules[1].id,)
            assert impact.affected_module_ids == tuple(item.id for item in modules)
            assert impact.module_patches[0].replacement.candidate_id == frame_alternate.id
            assert impact.module_patches[0].base_snapshot_hash == next(
                item["snapshot_hash"]
                for item in solution.module_snapshots
                if item["module_id"] == str(modules[0].id)
            )
            assert impact.profile_id == "impact-proposer-ro"
            assert await session.scalar(select(func.count()).select_from(JoinReceiptRow)) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetOperationRow)
                    .where(BudgetOperationRow.state == "settled")
                )
                == 1
            )
            assert await session.scalar(
                select(BudgetOperationRow.consumed_tokens).where(
                    BudgetOperationRow.kind == "model"
                )
            ) == 180
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "crash_point",
    ["after_wave", "after_one_child", "after_join", "after_domain"],
)
@pytest.mark.asyncio
async def test_reclaimed_parent_recovers_each_research_crash_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE jobs, artifacts CASCADE"))
            await session.commit()
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name=f"Recovery fixture {crash_point}",
                goal="Recover one committed research join",
                idempotency_key=f"recovery-project-{uuid4()}",
            )
            requirement, modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=project.revision,
                goal=project.goal,
                hard_constraints=("Use evidence",),
                preferences=("Keep it repairable",),
                available_resources=("Workshop",),
                unknowns=("Exact interfaces",),
                modules=(
                    {
                        "key": "frame",
                        "name": "Frame",
                        "responsibility": "Carry the system",
                    },
                    {
                        "key": "power",
                        "name": "Power",
                        "responsibility": "Supply safe power",
                    },
                ),
                idempotency_key=f"recovery-requirements-{uuid4()}",
            )
            basis_hash = sha256(
                f"{requirement.id}:{','.join(str(item.id) for item in modules)}".encode()
            ).hexdigest()
            root_job_id = await PostgresRuntime(session).create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision + 1,
                profile_id="research-orchestrator",
                profile_revision=1,
            )

        pause_reached = asyncio.Event()
        release_pause = asyncio.Event()
        original_create_wave = PostgresRuntime.create_delegation_wave
        original_commit_join = PostgresRuntime.commit_join
        original_complete_claim = PostgresRuntime.complete_claim

        if crash_point == "after_wave":

            async def pause_after_wave(
                runtime: PostgresRuntime,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                wave = await original_create_wave(runtime, *args, **kwargs)
                pause_reached.set()
                await release_pause.wait()
                return wave

            monkeypatch.setattr(
                PostgresRuntime,
                "create_delegation_wave",
                pause_after_wave,
            )
        elif crash_point == "after_join":

            async def pause_after_join(
                runtime: PostgresRuntime,
                *,
                join_group_id: Any,
                merged_proposal_ref: str,
            ) -> Any:
                receipt = await original_commit_join(
                    runtime,
                    join_group_id=join_group_id,
                    merged_proposal_ref=merged_proposal_ref,
                )
                if receipt is not None:
                    pause_reached.set()
                    await release_pause.wait()
                return receipt

            monkeypatch.setattr(PostgresRuntime, "commit_join", pause_after_join)
        elif crash_point == "after_domain":

            async def pause_after_domain(
                runtime: PostgresRuntime,
                *,
                claim: Any,
                status: JobStatus,
                result_ref: str | None = None,
                normalized_error: str | None = None,
            ) -> bool:
                if claim.job_id == root_job_id and status is JobStatus.SUCCEEDED:
                    pause_reached.set()
                    await release_pause.wait()
                return await original_complete_claim(
                    runtime,
                    claim=claim,
                    status=status,
                    result_ref=result_ref,
                    normalized_error=normalized_error,
                )

            monkeypatch.setattr(PostgresRuntime, "complete_claim", pause_after_domain)

        worker = ResearchWorker(
            session_factory=factory,
            artifact_root=tmp_path,
            model_factory=lambda: MagicMock(spec=BaseChatModel),
            search_backend_factory=FakeSearchBackend,
            github_backend_factory=FakeGitHubBackend,
            page_fetcher=FakePageFetcher(),
            agent_factory=fake_agent_factory,
            lease_seconds=10,
            poll_seconds=0.02,
        )
        parent_task = asyncio.create_task(worker.run_once(worker_id="parent-before-crash"))
        async with asyncio.timeout(10):
            while True:
                async with factory() as session:
                    child_count = await session.scalar(
                        select(func.count())
                        .select_from(JobRow)
                        .where(JobRow.parent_job_id == root_job_id)
                    )
                if child_count == 2:
                    break
                await asyncio.sleep(0.02)

        if crash_point == "after_wave":
            async with asyncio.timeout(10):
                await pause_reached.wait()
        elif crash_point == "after_one_child":
            assert await worker.run_once(worker_id="child-before-crash") is True
        else:
            await asyncio.gather(
                worker.run_once(worker_id="child-one"),
                worker.run_once(worker_id="child-two"),
            )
            async with asyncio.timeout(10):
                await pause_reached.wait()

        async with factory() as session:
            expected_receipts = 1 if crash_point in {"after_join", "after_domain"} else 0
            assert (
                await session.scalar(select(func.count()).select_from(JoinReceiptRow))
                == expected_receipts
            )
            decision_before_reclaim = await session.scalar(
                select(DecisionRequestRow.id).where(DecisionRequestRow.project_id == project.id)
            )
            assert (decision_before_reclaim is not None) is (crash_point == "after_domain")

            root = await session.get(JobRow, root_job_id)
            old_attempt = await session.scalar(
                select(AttemptRow).where(
                    AttemptRow.job_id == root_job_id,
                    AttemptRow.claim_generation == 1,
                )
            )
            assert root is not None and root.status == "running"
            assert old_attempt is not None
            assert root.lease_token is not None
            assert root.lease_owner is not None
            assert root.lease_expires_at is not None
            stale_claim = JobClaim(
                job_id=root.id,
                attempt_id=old_attempt.id,
                attempt_number=old_attempt.number,
                claim_generation=1,
                lease_token=root.lease_token,
                lease_owner=root.lease_owner,
                lease_expires_at=root.lease_expires_at,
                basis_hash=root.basis_hash,
                basis_project_revision=root.basis_project_revision,
                profile_id=root.profile_id,
                profile_revision=root.profile_revision,
            )

        parent_task.cancel()
        with suppress(asyncio.CancelledError):
            await parent_task
        monkeypatch.setattr(PostgresRuntime, "create_delegation_wave", original_create_wave)
        monkeypatch.setattr(PostgresRuntime, "commit_join", original_commit_join)
        monkeypatch.setattr(PostgresRuntime, "complete_claim", original_complete_claim)

        async with factory() as session:
            root = await session.get(JobRow, root_job_id)
            assert root is not None and root.status == "running"
            root.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        committed_before_crash = crash_point in {"after_join", "after_domain"}
        recovery_model_factory: Any
        if committed_before_crash:
            recovery_model_factory = MagicMock(
                side_effect=AssertionError("committed recovery must not invoke the model")
            )
        else:

            def build_recovery_model() -> BaseChatModel:
                return MagicMock(spec=BaseChatModel)

            recovery_model_factory = build_recovery_model
        recovery_worker = ResearchWorker(
            session_factory=factory,
            artifact_root=tmp_path,
            model_factory=recovery_model_factory,
            search_backend_factory=FakeSearchBackend,
            github_backend_factory=FakeGitHubBackend,
            page_fetcher=FakePageFetcher(),
            agent_factory=fake_agent_factory,
            lease_seconds=10,
            poll_seconds=0.02,
        )
        if committed_before_crash:
            assert await recovery_worker.run_once(worker_id="parent-after-crash") is True
            recovery_model_factory.assert_not_called()
        else:
            recovery_task = asyncio.create_task(
                recovery_worker.run_once(worker_id="parent-after-crash")
            )
            async with asyncio.timeout(10):
                while True:
                    async with factory() as session:
                        child_count = await session.scalar(
                            select(func.count())
                            .select_from(JobRow)
                            .where(JobRow.parent_job_id == root_job_id)
                        )
                    if child_count == 4:
                        break
                    await asyncio.sleep(0.02)
            await asyncio.gather(
                recovery_worker.run_once(worker_id="child-after-crash-one"),
                recovery_worker.run_once(worker_id="child-after-crash-two"),
            )
            assert await recovery_task is True

        async with factory() as session:
            root = await session.get(JobRow, root_job_id)
            assert root is not None
            assert root.status == "succeeded"
            assert root.current_generation == 2
            assert await session.scalar(
                select(func.count()).select_from(JobRow).where(JobRow.parent_job_id == root_job_id)
            ) == (2 if committed_before_crash else 4)
            assert await session.scalar(select(func.count()).select_from(DelegationRow)) == (
                2 if committed_before_crash else 4
            )
            groups = list(
                await session.scalars(
                    select(JoinGroupRow)
                    .where(JoinGroupRow.parent_job_id == root_job_id)
                    .order_by(JoinGroupRow.created_at)
                )
            )
            assert [item.status for item in groups] == (
                ["joined"] if committed_before_crash else ["cancelled", "joined"]
            )
            joined_child_ids = set(
                await session.scalars(
                    select(DelegationRow.child_job_id).where(
                        DelegationRow.join_group_id == groups[-1].id
                    )
                )
            )
            recovered_plan_tasks = list(
                await session.scalars(select(PlanTaskRow))
            )
            assert {item.status for item in recovered_plan_tasks} == {"succeeded"}
            assert {
                item.dispatched_job_id for item in recovered_plan_tasks
            } == joined_child_ids
            assert await session.scalar(select(func.count()).select_from(JoinReceiptRow)) == 1
            decision_after_reclaim = await session.scalar(
                select(DecisionRequestRow.id).where(DecisionRequestRow.project_id == project.id)
            )
            assert decision_after_reclaim is not None
            if decision_before_reclaim is not None:
                assert decision_after_reclaim == decision_before_reclaim
            expected_eligible_results = 3 if crash_point == "after_one_child" else 2
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AttemptResultRow)
                    .where(AttemptResultRow.disposition == "eligible")
                )
                == expected_eligible_results
            )
            root_attempt_statuses = list(
                await session.scalars(
                    select(AttemptRow.status)
                    .where(AttemptRow.job_id == root_job_id)
                    .order_by(AttemptRow.number)
                )
            )
            assert root_attempt_statuses == ["superseded", "succeeded"]
            stored_project = await session.get(ProjectRow, project.id)
            assert stored_project is not None and stored_project.revision == 3
            with pytest.raises(RuntimeConflictError, match="stale or terminal"):
                await PostgresRuntime(session).complete_claim(
                    claim=stale_claim,
                    status=JobStatus.SUCCEEDED,
                    result_ref=str(decision_after_reclaim),
                )
    finally:
        await engine.dispose()
