"""Focused integration tests for requirements-change proposals and impact previews."""

from __future__ import annotations

import os
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.application.agent_run_application import DEFAULT_RESEARCH_RUNTIME_BINDING
from aidison.infrastructure.agent_run_controls import AgentRunControlRequestStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from tests.integration.support import (
    approve_requirements_and_apply_initial_modules,
    module_discovery_model_factory,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def key_prefix():
    return str(uuid4())


def _headers(key_prefix: str, suffix: str, **extra) -> dict[str, str]:
    return {"Idempotency-Key": f"{key_prefix}:{suffix}", **extra}


def _stale_research_run(*, project_id, basis_project_revision: int) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"requirements-change-run-{uuid4()}",
        basis_hash=sha256(f"requirements-change:{uuid4()}".encode()).hexdigest(),
        basis_project_revision=basis_project_revision,
        runtime_binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
        thread_id=f"requirements-change-run-{uuid4()}",
    )


async def _create_project(client: httpx.AsyncClient, key_prefix: str) -> str:
    r = await client.post(
        "/api/projects",
        json={"name": "Requirements Change", "goal": "Verify governed rewrites"},
        headers=_headers(key_prefix, "project"),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _approve_requirements(
    client: httpx.AsyncClient, key_prefix: str, project_id: str
) -> None:
    await approve_requirements_and_apply_initial_modules(
        client,
        project_id=project_id,
        goal="Initial goal",
        idempotency_key_prefix=f"{key_prefix}:reqs",
    )


@pytest.mark.asyncio
async def test_requirements_change_proposal_lifecycle(key_prefix: str) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        api = create_app(factory, module_discovery_model_factory=module_discovery_model_factory)
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            current = await client.get(f"/api/projects/{project_id}")
            revision = current.json()["revision"]

            # Propose a rewrite → must stay proposed, revision unchanged.
            created = await client.post(
                f"/api/projects/{project_id}/requirements-change-proposals",
                json={
                    "target_goal": "Rewritten goal",
                    "hard_constraints": ["cheap"],
                    "summary": "Rewrite the project requirements",
                    "modules": [
                        {"key": "core", "name": "Core", "responsibility": "Core logic"},
                        {"key": "ui", "name": "UI", "responsibility": "User interface"},
                    ],
                },
                headers={**_headers(key_prefix, "propose-change"), "If-Match": f'"{revision}"'},
            )
            assert created.status_code == 201, created.text
            proposal_id = created.json()["requirements_change_proposal"]["id"]
            assert created.json()["requirements_change_proposal"]["status"] == "proposed"

            after_propose = await client.get(f"/api/projects/{project_id}")
            assert after_propose.json()["revision"] == revision
            assert (await client.get(f"/api/projects/{project_id}/modules")).json()[0][
                "key"
            ] == "core"

            # Reject → revision unchanged.
            rejected = await client.post(
                f"/api/requirements-change-proposals/{proposal_id}/resolve",
                json={"decision": "rejected"},
                headers={**_headers(key_prefix, "reject-change"), "If-Match": f'"{revision}"'},
            )
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["requirements_change_proposal"]["status"] == "rejected"
            assert rejected.json()["project_revision"] == revision

            # Propose again and apply → revision advances, new requirement + modules.
            created2 = await client.post(
                f"/api/projects/{project_id}/requirements-change-proposals",
                json={
                    "target_goal": "Rewritten goal",
                    "hard_constraints": ["cheap"],
                    "summary": "Rewrite the project requirements",
                    "modules": [
                        {"key": "core", "name": "Core", "responsibility": "Core logic"},
                        {"key": "ui", "name": "UI", "responsibility": "User interface"},
                    ],
                },
                headers={
                    **_headers(key_prefix, "propose-change-2"),
                    "If-Match": f'"{revision}"',
                },
            )
            assert created2.status_code == 201, created2.text
            proposal_id2 = created2.json()["requirements_change_proposal"]["id"]

            applied = await client.post(
                f"/api/requirements-change-proposals/{proposal_id2}/resolve",
                json={"decision": "applied"},
                headers={
                    **_headers(key_prefix, "apply-change"),
                    "If-Match": f'"{revision}"',
                },
            )
            assert applied.status_code == 200, applied.text
            assert applied.json()["requirements_change_proposal"]["status"] == "applied"
            assert applied.json()["project_revision"] == revision + 1

            after_apply = await client.get(f"/api/projects/{project_id}")
            assert after_apply.json()["revision"] == revision + 1
            assert after_apply.json()["goal"] == "Rewritten goal"
            module_keys = sorted(
                item["key"]
                for item in (await client.get(f"/api/projects/{project_id}/modules")).json()
            )
            assert module_keys == ["core", "ui"]

            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            proposals = snapshot.json()["requirements_change_proposals"]
            assert {item["status"] for item in proposals} == {"applied", "rejected"}

            events = await client.get(f"/api/projects/{project_id}/events")
            event_types = {event["type"] for event in events.json()}
            assert "requirements_change.proposed" in event_types
            assert "requirements_change.applied" in event_types
            assert "requirements_change.rejected" in event_types
            assert "requirements.approved" in event_types
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_applied_requirement_revision_invalidates_old_runs_and_acknowledges_basis_steering(
    key_prefix: str,
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

        api = create_app(factory, module_discovery_model_factory=module_discovery_model_factory)
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)
            project = await client.get(f"/api/projects/{project_id}")
            revision = project.json()["revision"]

            async with factory() as session:
                control = AgentRunControl(session)
                running = await control.create(
                    _stale_research_run(
                        project_id=project_id,
                        basis_project_revision=revision,
                    )
                )
                await session.commit()
                claim = await control.claim_next(worker_id="basis-worker", lease_seconds=60)
                assert claim is not None and claim.run_id == running.id
                queued = await control.create(
                    _stale_research_run(
                        project_id=project_id,
                        basis_project_revision=revision,
                    )
                )
                await session.commit()

            proposed = await client.post(
                f"/api/projects/{project_id}/requirements-change-proposals",
                json={
                    "target_goal": "Changed goal invalidates current research",
                    "summary": "Change the frozen requirement basis",
                    "modules": [
                        {"key": "core", "name": "Core", "responsibility": "Core logic"}
                    ],
                },
                headers={**_headers(key_prefix, "propose-basis"), "If-Match": f'"{revision}"'},
            )
            assert proposed.status_code == 201, proposed.text
            proposal_id = proposed.json()["requirements_change_proposal"]["id"]

            steering = await client.post(
                f"/api/projects/{project_id}/agent-runs/{running.id}/controls",
                json={
                    "kind": "basis_steering",
                    "basis_hash": running.basis_hash,
                    "change_request_ref": proposal_id,
                },
                headers={**_headers(key_prefix, "basis-steering"), "If-Match": f'"{revision}"'},
            )
            assert steering.status_code == 200, steering.text

            applied = await client.post(
                f"/api/requirements-change-proposals/{proposal_id}/resolve",
                json={"decision": "applied"},
                headers={**_headers(key_prefix, "apply-basis"), "If-Match": f'"{revision}"'},
            )
            assert applied.status_code == 200, applied.text
            assert set(applied.json()["invalidated_agent_run_ids"]) == {
                str(running.id),
                str(queued.id),
            }

            async with factory() as session:
                run_control = AgentRunControl(session)
                invalidated_running = await run_control.get(running.id)
                invalidated_queued = await run_control.get(queued.id)
                assert invalidated_running is not None
                assert invalidated_running.status is AgentRunStatus.RUNNING
                assert invalidated_running.cancel_requested is True
                assert invalidated_queued is not None
                assert invalidated_queued.status is AgentRunStatus.CANCELLED
                [request] = await AgentRunControlRequestStore(session).list_for_run(
                    agent_run_id=running.id
                )
                assert request.status.value == "acknowledged"

            events = await client.get(f"/api/projects/{project_id}/events")
            assert events.status_code == 200, events.text
            invalidations = [
                event for event in events.json() if event["type"] == "agent_run.basis_invalidated"
            ]
            assert {event["payload"]["agent_run_id"] for event in invalidations} == {
                str(running.id),
                str(queued.id),
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approve_requirements_persists_clarified_context_end_to_end(key_prefix: str) -> None:
    """POST /requirements with usage/budget/skill context is read back after approval."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        api = create_app(factory, module_discovery_model_factory=module_discovery_model_factory)
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            project_id = await _create_project(client, key_prefix)

            await approve_requirements_and_apply_initial_modules(
                client,
                project_id=project_id,
                goal="自动拾取物料的机械臂",
                idempotency_key_prefix=f"{key_prefix}:reqs-context",
                requirement_context={
                    "available_resources": ["已有机械臂底座"],
                    "usage_context": "用于流水线自动分拣物料",
                    "budget_context": "总成本控制在 2 万元以内",
                    "skill_context": "团队具备机械与电气基础",
                },
            )

            # Read the persisted revision back from the canonical snapshot.
            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            body = snapshot.json()
            revisions = body["requirements"]
            assert len(revisions) == 1
            assert revisions[0]["usage_context"] == "用于流水线自动分拣物料"
            assert revisions[0]["budget_context"] == "总成本控制在 2 万元以内"
            assert revisions[0]["skill_context"] == "团队具备机械与电气基础"
            assert revisions[0]["available_resources"] == ["已有机械臂底座"]

            # budget_context is only a requirement fact: no spend-budget artifact.
            assert body["spend_budget"] is None
            assert body["spend_budget_proposals"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_flush_produces_change_impact_preview(key_prefix: str) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        api = create_app(factory, module_discovery_model_factory=module_discovery_model_factory)
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            current = await client.get(f"/api/projects/{project_id}")
            revision = current.json()["revision"]
            module_id = (await client.get(f"/api/projects/{project_id}/modules")).json()[0]["id"]

            # Record an adjustment (set a parameter on the module).
            adjustment = await client.post(
                f"/api/projects/{project_id}/modules/{module_id}/adjustments",
                json={"kind": "set_parameter", "target": {"key": "weight_kg", "value": 2}},
                headers={
                    **_headers(key_prefix, "adjust"),
                    "If-Match": f'"{revision}"',
                },
            )
            assert adjustment.status_code == 201, adjustment.text
            batch_id = adjustment.json()["adjustment"]["batch_id"]

            # Flush → the response carries a read-only impact preview.
            flushed = await client.post(
                f"/api/adjustment-batches/{batch_id}/flush",
                headers={
                    **_headers(key_prefix, "flush"),
                    "If-Match": f'"{revision + 1}"',
                },
            )
            assert flushed.status_code == 200, flushed.text
            body = flushed.json()
            assert body["adjustment_batch"]["status"] == "flushed"
            preview = body["change_impact_preview"]
            assert preview is not None
            assert preview["affected_module_ids"] == [module_id]
            assert preview["direct_affected_module_ids"] == [module_id]

            # The preview is listable and stored in the snapshot.
            previews = await client.get(f"/api/projects/{project_id}/change-impact-previews")
            assert previews.status_code == 200, previews.text
            assert previews.json()[0]["id"] == preview["id"]

            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.json()["change_impact_previews"][0]["id"] == preview["id"]

            events = await client.get(f"/api/projects/{project_id}/events")
            assert "change_impact_preview.generated" in {event["type"] for event in events.json()}
    finally:
        await engine.dispose()
