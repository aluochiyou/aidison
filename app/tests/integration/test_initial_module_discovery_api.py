from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory

pytestmark = pytest.mark.integration


def _module_discovery_model_factory() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "Separate sensing from safe power and control.",
                    "modules": [
                        {
                            "key": "sensing",
                            "name": "Sensing",
                            "responsibility": "Measure the requested environment.",
                        },
                        {
                            "key": "power_control",
                            "name": "Power and control",
                            "responsibility": "Safely power and coordinate sensing.",
                            "dependency_keys": ["sensing"],
                        },
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


def _research_strategy_model_factory() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def reply(messages: list[dict[str, str]]) -> AIMessage:
        context = json.loads(messages[-1]["content"])
        scope = context["allowed_scope"]
        is_deep = context["research_depth"] == "deep"
        generation_mode = context["strategy_generation_mode"]
        scope_by_id = {item["id"]: item for item in scope}
        tasks: list[dict[str, object]] = []
        for item in scope:
            if generation_mode == "global_skeleton":
                tasks.append(
                    {
                        "task_key": f"{item['key']}.skeleton",
                        "title": f"Establish {item['name']} research boundary",
                        "objective": f"Create a bounded research seed for {item['name']}.",
                        "module_ids": [item["id"]],
                        "depends_on_task_keys": [],
                        "priority": "must",
                        "expected_outputs": ["evidence", "candidate"],
                        "stop_conditions": ["Hand off a bounded module research seed."],
                    }
                )
                continue
            upstream_task_keys = [
                f"{scope_by_id[dependency_id]['key']}.research"
                for dependency_id in item["dependencies"]
                if dependency_id in scope_by_id
            ]
            if is_deep and item["deep_decomposition_required"]:
                constraint_key = f"{item['key']}.constraints"
                tasks.extend(
                    (
                        {
                            "task_key": constraint_key,
                            "title": f"Establish {item['name']} interface constraints",
                            "objective": f"Find source-backed constraints for {item['name']}.",
                            "module_ids": [item["id"]],
                            "depends_on_task_keys": upstream_task_keys,
                            "priority": "must",
                            "expected_outputs": ["evidence", "constraint"],
                            "stop_conditions": ["Record an interface constraint or explicit gap."],
                            "research_lenses": ["approved interface specifications"],
                        },
                        {
                            "task_key": f"{item['key']}.compatibility",
                            "title": f"Validate {item['name']} compatibility",
                            "objective": (
                                f"Compare {item['name']} options against the established "
                                "interface constraints."
                            ),
                            "module_ids": [item["id"]],
                            "depends_on_task_keys": [constraint_key],
                            "priority": "must",
                            "expected_outputs": ["evidence", "candidate", "compatibility"],
                            "stop_conditions": [
                                "Record a source-backed compatibility result or gap."
                            ],
                            "research_lenses": ["failure modes and option trade-offs"],
                        },
                    )
                )
                continue
            tasks.append(
                {
                    "task_key": f"{item['key']}.research",
                    "title": f"Research {item['name']}",
                    "objective": f"Find evidence for {item['name']}.",
                    "module_ids": [item["id"]],
                    "depends_on_task_keys": upstream_task_keys,
                    "priority": "must" if is_deep else "should",
                    "expected_outputs": (
                        ["evidence", "candidate", "compatibility"]
                        if item["dependencies"]
                        else ["evidence", "candidate"]
                    ),
                    "stop_conditions": ["Record evidence or an explicit gap."],
                    "research_lenses": [
                        "approved specifications",
                        "failure modes and option trade-offs",
                    ] if is_deep else [],
                }
            )
        return AIMessage(
            content=json.dumps(
                {
                    "summary": "Research each reviewed module through a typed strategy.",
                    "decision_notes": [
                        "Compare module candidates against the approved indoor budget and "
                        "beginner-maintenance constraints."
                    ],
                    "scope_module_ids": [item["id"] for item in scope],
                    "tasks": tasks,
                    "source_strategy": "primary",
                }
            )
        )

    bound.ainvoke = AsyncMock(side_effect=reply)
    model.bind.return_value = bound
    return model


@pytest.mark.asyncio
async def test_initial_module_discovery_requires_explicit_review_before_research_plan() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    api = create_app(
        factory,
        module_discovery_model_factory=_module_discovery_model_factory,
        research_strategy_model_factory=_research_strategy_model_factory,
    )
    transport = httpx.ASGITransport(app=api)
    key = str(uuid4())
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Discovery fixture", "goal": "Monitor two indoor plants"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            assert created.status_code == 201
            project_id = created.json()["id"]
            direct_modules = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={
                    "goal": "Monitor two indoor plants",
                    "modules": [
                        {
                            "key": "manual",
                            "name": "Manual module",
                            "responsibility": "Must not bypass discovery.",
                        }
                    ],
                },
                headers={"Idempotency-Key": f"{key}:direct-modules", "If-Match": '"1"'},
            )
            assert direct_modules.status_code == 409
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={
                    "goal": "Monitor two indoor plants",
                    "usage_context": "Indoor windowsill",
                    "budget_context": "CNY 500 maximum",
                    "skill_context": "Beginner soldering",
                },
                headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
            )
            assert requirements.status_code == 200, requirements.text
            assert requirements.json()["modules"] == []
            replayed_requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={
                    "goal": "Monitor two indoor plants",
                    "usage_context": "Indoor windowsill",
                    "budget_context": "CNY 500 maximum",
                    "skill_context": "Beginner soldering",
                },
                headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
            )
            assert replayed_requirements.status_code == 200, replayed_requirements.text
            assert replayed_requirements.json()["requirement"]["id"] == requirements.json()[
                "requirement"
            ]["id"]
            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.json()["project"]["active_blueprint_id"] is None
            assert snapshot.json()["modules"] == []

            blocked_plan = await client.post(
                f"/api/projects/{project_id}/execution-plans/research-default",
                headers={"Idempotency-Key": f"{key}:blocked-plan", "If-Match": '"2"'},
            )
            assert blocked_plan.status_code == 404

            bypass = await client.post(
                f"/api/projects/{project_id}/reshape-proposals",
                json={
                    "target_goal": "Monitor two indoor plants",
                    "summary": "Bypass initial discovery",
                    "new_modules": [
                        {
                            "key": "manual",
                            "name": "Manual",
                            "responsibility": "Must not bypass discovery.",
                        }
                    ],
                },
                headers={"Idempotency-Key": f"{key}:bypass", "If-Match": '"2"'},
            )
            assert bypass.status_code == 409

            requirements_change_bypass = await client.post(
                f"/api/projects/{project_id}/requirements-change-proposals",
                json={
                    "target_goal": "Monitor two indoor plants",
                    "summary": "Bypass initial discovery through requirements changes",
                    "modules": [
                        {
                            "key": "manual",
                            "name": "Manual module",
                            "responsibility": "Must not bypass discovery.",
                        }
                    ],
                },
                headers={"Idempotency-Key": f"{key}:change-bypass", "If-Match": '"2"'},
            )
            assert requirements_change_bypass.status_code == 409

            discovery = await client.post(
                f"/api/projects/{project_id}/module-discovery",
                headers={"Idempotency-Key": f"{key}:discover", "If-Match": '"2"'},
            )
            assert discovery.status_code == 201, discovery.text
            proposal = discovery.json()["reshape_proposal"]
            assert proposal["status"] == "proposed"
            replayed = await client.post(
                f"/api/projects/{project_id}/module-discovery",
                headers={"Idempotency-Key": f"{key}:discover-again", "If-Match": '"2"'},
            )
            assert replayed.status_code == 201, replayed.text
            assert replayed.json()["reshape_proposal"]["id"] == proposal["id"]
            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.json()["modules"] == []
            assert snapshot.json()["project"]["active_blueprint_id"] is None
            assert snapshot.json()["workspace"]["next_actions"][0]["kind"] == "review_reshape"

            applied = await client.post(
                f"/api/reshape-proposals/{proposal['id']}/resolve",
                json={"decision": "applied"},
                headers={"Idempotency-Key": f"{key}:apply", "If-Match": '"2"'},
            )
            assert applied.status_code == 200, applied.text
            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert [item["key"] for item in snapshot.json()["modules"]] == [
                "sensing",
                "power_control",
            ]
            assert snapshot.json()["project"]["active_blueprint_id"] is not None

            plan = await client.post(
                f"/api/projects/{project_id}/execution-plans/research-strategy",
                json={},
                headers={"Idempotency-Key": f"{key}:plan", "If-Match": '"3"'},
            )
            assert plan.status_code == 201, plan.text
            execution_plan = plan.json()["execution_plan"]
            assert execution_plan["research_depth"] == "deep"
            assert execution_plan["requires_independent_verification"] is True
            assert execution_plan["max_token_budget"] == 200_000_000
            assert execution_plan["max_duration_seconds"] == 36_000
            assert execution_plan["status"] == "proposed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_initial_module_discovery_calls_the_model_once() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    model = _module_discovery_model_factory()
    bound = model.bind.return_value
    original_ainvoke = bound.ainvoke

    async def slow_ainvoke(*args: object, **kwargs: object) -> AIMessage:
        await asyncio.sleep(0.05)
        return await original_ainvoke(*args, **kwargs)

    bound.ainvoke = AsyncMock(side_effect=slow_ainvoke)
    api = create_app(factory, module_discovery_model_factory=lambda: model)
    transport = httpx.ASGITransport(app=api)
    key = str(uuid4())
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Concurrent discovery", "goal": "Monitor a plant"},
                headers={"Idempotency-Key": f"{key}:project"},
            )
            project_id = created.json()["id"]
            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json={"goal": "Monitor a plant"},
                headers={"Idempotency-Key": f"{key}:requirements", "If-Match": '"1"'},
            )
            assert requirements.status_code == 200, requirements.text

            brief = "Keep sensing and safety boundaries independently reviewable."
            first, second = await asyncio.gather(
                client.post(
                    f"/api/projects/{project_id}/module-discovery",
                    json={
                        "planning_brief": brief,
                        "structure_depth": "deep",
                    },
                    headers={"Idempotency-Key": f"{key}:discover-1", "If-Match": '"2"'},
                ),
                client.post(
                    f"/api/projects/{project_id}/module-discovery",
                    json={
                        "planning_brief": brief,
                        "structure_depth": "deep",
                    },
                    headers={"Idempotency-Key": f"{key}:discover-2", "If-Match": '"2"'},
                ),
            )
            assert first.status_code == 201, first.text
            assert second.status_code == 201, second.text
            assert first.json()["reshape_proposal"]["id"] == second.json()["reshape_proposal"]["id"]
            bound.ainvoke.assert_awaited_once()
            messages = bound.ainvoke.await_args.args[0]
            planning_context = json.loads(messages[1]["content"])
            assert planning_context["structure_depth"] == "deep"
            assert planning_context["planning_brief"] == brief
    finally:
        await engine.dispose()
