from __future__ import annotations

import os
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_run_controls import AgentRunControlRequestStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.control_requests import AgentRunControlRequest, ControlRequestKind
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _binding() -> RuntimeBinding:
    return RuntimeBinding(
        runtime_family=RuntimeFamily.LANGGRAPH_V1,
        runtime_revision="runtime-v1",
        graph_key="research",
        graph_revision="research-v1",
        state_schema_version="research-state-v1",
        profile_binding_ref="profile://research/1",
        policy_binding_ref="policy://research/1",
    )


def _run(project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"agent-run-control-{uuid4()}",
        basis_hash=sha256(b"agent-run-control").hexdigest(),
        basis_project_revision=1,
        runtime_binding=_binding(),
        thread_id=f"agent-run-control-{uuid4()}",
    )


@pytest.mark.asyncio
async def test_cancel_queued_run_is_terminal_and_never_claimable() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Queued AgentRun cancellation fixture",
                goal="Verify queued cancellation cannot be claimed",
                idempotency_key=f"project-{uuid4()}",
            )
            control = AgentRunControl(session)
            run = await control.create(_run(project.id))
            await session.commit()

            cancelled = await control.request_cancel(run_id=run.id)
            assert cancelled.status is AgentRunStatus.CANCELLED
            assert cancelled.cancel_requested is True
            assert cancelled.completed_at is not None
            await session.commit()

            assert await control.claim_next(worker_id="worker", lease_seconds=60) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_running_cancel_requires_current_worker_safe_point() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Running AgentRun cancellation fixture",
                goal="Verify a worker owns the terminal cancellation boundary",
                idempotency_key=f"project-{uuid4()}",
            )
            control = AgentRunControl(session)
            run = await control.create(_run(project.id))
            await session.commit()
            claim = await control.claim_next(worker_id="worker", lease_seconds=60)
            assert claim is not None
            await session.commit()

            requested = await control.request_cancel(run_id=run.id)
            assert requested.status is AgentRunStatus.RUNNING
            assert requested.cancel_requested is True
            await session.commit()

            cancelled = await control.acknowledge_cancel_at_safe_point(claim=claim)
            assert cancelled.status is AgentRunStatus.CANCELLED
            assert cancelled.completed_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_control_request_is_idempotent_and_acknowledged_separately() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun steering fixture",
                goal="Verify typed control requests are durable and separate from a run",
                idempotency_key=f"project-{uuid4()}",
            )
            run = await AgentRunControl(session).create(_run(project.id))
            request = AgentRunControlRequest(
                agent_run_id=run.id,
                kind=ControlRequestKind.RUNTIME_STEERING,
                basis_hash=run.basis_hash,
                payload={"instruction": "prioritize independently verified evidence"},
                idempotency_key=f"control-{uuid4()}",
            )
            store = AgentRunControlRequestStore(session)
            created = await store.request(request)
            replayed = await store.request(request.model_copy(update={"id": uuid4()}))
            assert replayed.id == created.id
            assert (await store.list_for_run(agent_run_id=run.id)) == (created,)
            acknowledged = await store.acknowledge(created.id)
            assert acknowledged.status.value == "acknowledged"
            assert acknowledged.acknowledged_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_control_commands_are_versioned_visible_and_cancel_is_event_idempotent() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun command API fixture",
                goal="Verify versioned control commands and user projections",
                idempotency_key=f"project-{uuid4()}",
            )
            run = await AgentRunControl(session).create(_run(project.id))
            await session.commit()

        app = create_app(factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Idempotency-Key": f"steer-{uuid4()}", "If-Match": '"1"'}
            invalid = await client.post(
                f"/api/projects/{project.id}/agent-runs/{run.id}/controls",
                json={"kind": "runtime_steering", "basis_hash": run.basis_hash},
                headers=headers,
            )
            assert invalid.status_code == 422, invalid.text

            requested = await client.post(
                f"/api/projects/{project.id}/agent-runs/{run.id}/controls",
                json={
                    "kind": "runtime_steering",
                    "basis_hash": run.basis_hash,
                    "instruction": "prioritize independently verified evidence",
                },
                headers={"Idempotency-Key": f"steer-{uuid4()}", "If-Match": '"1"'},
            )
            assert requested.status_code == 200, requested.text
            assert requested.json()["control_request"]["status"] == "requested"

            first_cancel = await client.post(
                f"/api/projects/{project.id}/agent-runs/{run.id}/cancel",
                headers={"Idempotency-Key": f"cancel-{uuid4()}", "If-Match": '"1"'},
            )
            assert first_cancel.status_code == 200, first_cancel.text
            assert first_cancel.json()["agent_run"]["status"] == "cancelled"
            repeated_cancel = await client.post(
                f"/api/projects/{project.id}/agent-runs/{run.id}/cancel",
                headers={"Idempotency-Key": f"cancel-{uuid4()}", "If-Match": '"1"'},
            )
            assert repeated_cancel.status_code == 200, repeated_cancel.text

            snapshot = await client.get(f"/api/projects/{project.id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            [visible_run] = snapshot.json()["agent_runs"]
            assert visible_run["id"] == str(run.id)
            assert visible_run["basis_hash"] == run.basis_hash
            assert visible_run["cancel_requested"] is True
            assert visible_run["control_requests"][0]["kind"] == "runtime_steering"

            events = await client.get(f"/api/projects/{project.id}/events")
            assert events.status_code == 200, events.text
            cancellation_events = [
                event for event in events.json() if event["type"] == "agent_run.cancelled"
            ]
            assert len(cancellation_events) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_acknowledged_pre_dispatch_pause_can_resume_once_through_api() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Paused AgentRun resume API fixture",
                goal="Verify only a worker-acknowledged pause can requeue a run",
                idempotency_key=f"project-{uuid4()}",
            )
            control = AgentRunControl(session)
            run = await control.create(_run(project.id))
            await session.commit()
            claim = await control.claim_next(worker_id="worker", lease_seconds=60)
            assert claim is not None
            pause = await AgentRunControlRequestStore(session).request(
                AgentRunControlRequest(
                    agent_run_id=run.id,
                    kind=ControlRequestKind.PAUSE,
                    basis_hash=run.basis_hash,
                    idempotency_key=f"pause-{uuid4()}",
                )
            )
            await AgentRunControlRequestStore(session).acknowledge(pause.id)
            paused = await control.wait_for_decision(claim=claim)
            assert paused.status is AgentRunStatus.WAITING
            await session.commit()

        app = create_app(factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Idempotency-Key": f"resume-{uuid4()}", "If-Match": '"1"'}
            resumed = await client.post(
                f"/api/projects/{project.id}/agent-runs/{run.id}/resume",
                headers=headers,
            )
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["agent_run"]["status"] == "queued"

            repeated = await client.post(
                f"/api/projects/{project.id}/agent-runs/{run.id}/resume",
                headers={"Idempotency-Key": f"resume-{uuid4()}", "If-Match": '"1"'},
            )
            assert repeated.status_code == 200, repeated.text

            events = await client.get(f"/api/projects/{project.id}/events")
            assert events.status_code == 200, events.text
            resumed_events = [
                event for event in events.json() if event["type"] == "agent_run.resumed"
            ]
            assert len(resumed_events) == 1
    finally:
        await engine.dispose()
