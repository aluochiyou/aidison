"""End-to-end authorization test for AI-planned Research strategy execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory

pytestmark = pytest.mark.integration


def _module_discovery_model() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "Separate power from flight control.",
                    "modules": [
                        {
                            "key": "power",
                            "name": "Power",
                            "responsibility": "Provide safe electrical power.",
                        },
                        {
                            "key": "control",
                            "name": "Flight control",
                            "responsibility": "Control the aircraft safely.",
                            "dependency_keys": ["power"],
                        },
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


def _strategy_model() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def reply(messages: list[dict[str, str]]) -> AIMessage:
        context = json.loads(messages[-1]["content"])
        scope = context["allowed_scope"]
        mode = context.get("strategy_generation_mode", "final")
        if mode == "global_skeleton":
            tasks = [
                {
                    "task_key": f"{module['key']}.seed",
                    "title": f"Plan {module['name']} research",
                    "objective": f"Identify the research focus for {module['name']}.",
                    "module_ids": [module["id"]],
                    "depends_on_task_keys": [],
                    "priority": "must",
                    "expected_outputs": ["evidence", "constraint"],
                    "stop_conditions": ["Record a bounded research focus or gap."],
                    "research_lenses": ["approved specifications"],
                }
                for module in scope
            ]
            return AIMessage(
                content=json.dumps(
                    {
                        "schema_version": "research-strategy-v1",
                        "summary": "Establish module research seeds before local expansion.",
                        "decision_notes": ["Dependent module research follows power constraints."],
                        "scope_module_ids": [item["id"] for item in scope],
                        "tasks": tasks,
                        "deferred_questions": ["Final integration depends on admitted evidence."],
                        "risk_notes": ["Do not treat the strategy as product evidence."],
                        "source_strategy": "primary",
                    }
                )
            )
        if mode == "module_detail":
            [module] = scope
            tasks = [
                {
                    "task_key": "constraints",
                    "title": f"Establish {module['name']} constraints",
                    "objective": f"Establish source-backed constraints for {module['name']}.",
                    "module_ids": [module["id"]],
                    "depends_on_task_keys": [],
                    "priority": "must",
                    "expected_outputs": ["evidence", "constraint"],
                    "stop_conditions": ["A source-backed constraint or gap is recorded."],
                    "research_lenses": ["approved interface specifications"],
                },
                {
                    "task_key": "compatibility",
                    "title": f"Validate {module['name']} compatibility",
                    "objective": f"Compare {module['name']} options against constraints.",
                    "module_ids": [module["id"]],
                    "depends_on_task_keys": ["constraints"],
                    "priority": "must",
                    "expected_outputs": ["evidence", "candidate", "compatibility"],
                    "stop_conditions": ["A compatibility conclusion or gap is recorded."],
                    "research_lenses": ["failure modes and option trade-offs"],
                },
            ]
            return AIMessage(
                content=json.dumps(
                    {
                        "schema_version": "research-strategy-v1",
                        "summary": f"Expand {module['name']} locally.",
                        "scope_module_ids": [module["id"]],
                        "tasks": tasks,
                        "source_strategy": "primary",
                    }
                )
            )
        tasks = []
        for index, module in enumerate(scope):
            if module["deep_decomposition_required"]:
                constraint_key = f"{module['key']}.constraints"
                tasks.extend(
                    (
                        {
                            "task_key": constraint_key,
                            "title": f"Establish {module['name']} interface constraints",
                            "objective": (
                                f"Establish source-backed interface constraints for "
                                f"{module['name']} from its prerequisite module."
                            ),
                            "module_ids": [module["id"]],
                            "depends_on_task_keys": [tasks[0]["task_key"]],
                            "priority": "must",
                            "expected_outputs": ["evidence", "constraint"],
                            "stop_conditions": [
                                "A source-backed interface constraint or open gap is recorded."
                            ],
                            "research_lenses": ["approved interface specifications"],
                        },
                        {
                            "task_key": f"{module['key']}.compatibility",
                            "title": f"Validate {module['name']} compatibility",
                            "objective": (
                                f"Compare source-backed {module['name']} candidates against "
                                "the established interface constraints."
                            ),
                            "module_ids": [module["id"]],
                            "depends_on_task_keys": [constraint_key],
                            "priority": "must",
                            "expected_outputs": ["evidence", "candidate", "compatibility"],
                            "stop_conditions": [
                                "A source-backed compatibility conclusion or open gap is recorded."
                            ],
                            "research_lenses": ["failure modes and option trade-offs"],
                        },
                    )
                )
                continue
            task_key = f"{module['key']}.research"
            tasks.append(
                {
                    "task_key": task_key,
                    "title": f"Research {module['name']}",
                    "objective": (
                        f"Find source-backed options and constraints for {module['name']}."
                    ),
                    "module_ids": [module["id"]],
                    "depends_on_task_keys": [] if index == 0 else [tasks[0]["task_key"]],
                    "priority": "must",
                    "expected_outputs": (
                        ["evidence", "candidate", "compatibility"]
                        if module["dependencies"]
                        else ["evidence", "candidate"]
                    ),
                    "stop_conditions": ["A source-backed option and open gap are recorded."],
                    "research_lenses": [
                        "approved specifications",
                        "failure modes and option trade-offs",
                    ] if context["research_depth"] == "deep" else [],
                }
            )
        return AIMessage(
            content=json.dumps(
                {
                    "schema_version": "research-strategy-v1",
                    "summary": "Research electrical constraints before dependent control choices.",
                    "decision_notes": ["Control research depends on power assumptions."],
                    "scope_module_ids": [item["id"] for item in scope],
                    "tasks": tasks,
                    "deferred_questions": ["Final integration depends on admitted evidence."],
                    "risk_notes": ["Do not treat the strategy as product evidence."],
                    "source_strategy": "primary",
                }
            )
        )

    bound.ainvoke = AsyncMock(side_effect=reply)
    model.bind.return_value = bound
    return model


@pytest.mark.asyncio
async def test_approved_strategy_freezes_scope_and_compiles_its_task_dag(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    strategy_model = _strategy_model()
    api = create_app(
        factory,
        artifact_root=tmp_path,
        module_discovery_model_factory=_module_discovery_model,
        research_strategy_model_factory=lambda: strategy_model,
    )
    key = str(uuid4())
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Strategy fixture", "goal": "Build a safe indoor drone"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert created.status_code == 201, created.text
            project_id = created.json()["id"]
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={"goal": "Build a safe indoor drone", "budget_context": "CNY 2,000"},
                headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
            )
            assert requirements.status_code == 200, requirements.text
            discovery = await client.post(
                f"/api/projects/{project_id}/module-discovery",
                headers={"Idempotency-Key": f"{key}:discovery", "If-Match": '"2"'},
            )
            assert discovery.status_code == 201, discovery.text
            reshape = discovery.json()["reshape_proposal"]
            applied = await client.post(
                f"/api/reshape-proposals/{reshape['id']}/resolve",
                json={"decision": "applied"},
                headers={"Idempotency-Key": f"{key}:apply", "If-Match": '"2"'},
            )
            assert applied.status_code == 200, applied.text

            retired = await client.post(
                f"/api/projects/{project_id}/execution-plans/research-default",
                headers={"Idempotency-Key": f"{key}:retired-default", "If-Match": '"3"'},
            )
            assert retired.status_code == 404, retired.text
            manual = await client.post(
                f"/api/projects/{project_id}/execution-plans",
                headers={"Idempotency-Key": f"{key}:retired-manual", "If-Match": '"3"'},
            )
            assert manual.status_code == 404, manual.text

            proposed = await client.post(
                f"/api/projects/{project_id}/execution-plans/research-strategy",
                json={
                    "objective": (
                        "First establish power limits, then validate control compatibility."
                    ),
                    "max_concurrency": 2,
                    "max_token_budget": 18_000,
                    "research_depth": "deep",
                },
                headers={"Idempotency-Key": f"{key}:strategy", "If-Match": '"3"'},
            )
            assert proposed.status_code == 201, proposed.text
            assert proposed.json()["execution_plan"]["requires_independent_verification"] is True
            plan = proposed.json()["execution_plan"]
            assert plan["status"] == "proposed"
            assert plan["research_strategy"]["tasks"][1]["depends_on_task_keys"] == [
                "power.power.seed"
            ]
            assert plan["research_strategy"]["tasks"][2]["depends_on_task_keys"] == [
                "control.constraints"
            ]
            assert plan["max_concurrency"] == 2
            assert plan["research_depth"] == "deep"
            snapshot_before_approval = await client.get(f"/api/projects/{project_id}/snapshot")
            assert len(snapshot_before_approval.json()["agent_runs"]) == 0

            approved = await client.post(
                f"/api/execution-plans/{plan['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": plan["scope_hash"],
                    "enqueue_research": True,
                },
                headers={"Idempotency-Key": f"{key}:approve", "If-Match": '"3"'},
            )
            assert approved.status_code == 200, approved.text
            run_payload = approved.json()["agent_run"]

        async with factory() as session:
            run = await AgentRunControl(session).get(UUID(run_payload["id"]))
            assert run is not None
            assert run.run_contract_ref is not None
            assert run.coverage_contract_ref is not None
            artifacts = ContentAddressedArtifactStore(session, tmp_path)
            run_contract = await artifacts.read_json_ref(
                project_id=UUID(project_id),
                basis_hash=run.basis_hash,
                ref=run.run_contract_ref,
                expected_kind="research_run_contract",
            )
            coverage = await artifacts.read_json_ref(
                project_id=UUID(project_id),
                basis_hash=run.basis_hash,
                ref=run.coverage_contract_ref,
                expected_kind="research_coverage_contract",
            )
            assert run_contract["max_concurrency"] == 2
            assert run_contract["max_token_budget"] == 18_000
            assert run_contract["max_duration_seconds"] == 36_000
            assert run_contract["execution_policy"] == {
                "policy_version": "research-execution-policy-v2",
                "collection": {
                    "profile": "deep",
                    "max_queries": None,
                    "max_documents_total": None,
                    "max_documents_per_query": None,
                    "search_depth": "advanced",
                },
                "adaptive": {
                    "max_patch_revisions": None,
                    "max_total_tasks": None,
                    "max_tasks_per_patch": None,
                    "max_consecutive_no_progress": None,
                    "partial_delivery_allowed": False,
                },
                "minimum_evidence_sources_for_must": 2,
                "minimum_evidence_origins_for_must": 2,
            }
            assert [item["task_key"] for item in run_contract["research_strategy"]["tasks"]] == [
                "power.power.seed",
                "control.constraints",
                "control.compatibility",
            ]
            assert {item["key"] for item in coverage["keys"]} >= {
                "strategy.power.power.seed",
                "strategy.control.constraints",
                "strategy.control.compatibility",
            }
        assert strategy_model.bind.return_value.ainvoke.await_count == 2
    finally:
        await engine.dispose()
