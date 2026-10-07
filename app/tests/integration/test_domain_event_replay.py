from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from aidison.application.event_replay import ExecutionPlanShadowProjectionService
from aidison.application.service import ProjectApplication, canonical_hash
from aidison.domain.events import replay_execution_plan
from aidison.domain.models import ExecutionPlanProposal, ExecutionPlanStatus
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import DomainEventRow
from aidison.infrastructure.replay_snapshots import EventReplaySnapshotRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import CoordinationMode

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_execution_plan_relation_and_versioned_events_rebuild_the_same_state() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key = str(uuid4())
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with factory() as session:
            store = PostgresDomainStore(session)
            app = ProjectApplication(store)
            project = await app.create_project(
                name="Event replay fixture",
                goal="Rebuild an approved plan without a model or tool call",
                idempotency_key=f"{key}:project",
            )
            requirement, modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=1,
                goal=project.goal,
                hard_constraints=("evidence required",),
                preferences=(),
                available_resources=(),
                unknowns=(),
                modules=(),
                idempotency_key=f"{key}:requirements",
            )
            current = await store.get_project(project.id)
            assert current is not None
            proposal = await app.propose_execution_plan(
                proposal=ExecutionPlanProposal(
                    project_id=project.id,
                    basis_hash=canonical_hash(requirement.id, modules),
                    objective="Research a versioned execution plan",
                    work_summary=("collect evidence",),
                    allowed_coordination_modes=(CoordinationMode.DECOMPOSE,),
                    max_concurrency=1,
                    max_token_budget=4_000,
                ),
                expected_project_revision=current.revision,
                idempotency_key=f"{key}:propose",
            )
            approved = await app.resolve_execution_plan(
                proposal_id=proposal.id,
                decision=ExecutionPlanStatus.APPROVED,
                scope_hash=proposal.scope_hash or "",
                expected_project_revision=current.revision,
                idempotency_key=f"{key}:resolve",
            )

        async with factory() as session:
            store = PostgresDomainStore(session)
            events = await store.list_aggregate_events(
                project.id,
                aggregate_type="execution_plan",
                aggregate_id=proposal.id,
            )
            replayed = replay_execution_plan(list(events))
            stored = await store.get_execution_plan_proposal(proposal.id)
            legacy_event = await session.scalar(
                select(DomainEventRow).where(
                    DomainEventRow.project_id == project.id,
                    DomainEventRow.event_type == "project.created",
                )
            )

            assert stored is not None
            assert replayed.proposal == stored == approved
            assert [event.aggregate_version for event in events] == [1, 2]
            assert all(event.schema_version == 1 for event in events)
            assert legacy_event is not None
            assert legacy_event.schema_version == 0
            assert legacy_event.aggregate_id is None
            assert legacy_event.payload_hash is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_verified_snapshot_tail_replay_matches_relation_after_plan_resolution() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key = str(uuid4())
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with factory() as session:
            store = PostgresDomainStore(session)
            app = ProjectApplication(store)
            project = await app.create_project(
                name="Snapshot replay fixture",
                goal="Verify snapshot plus event tail against the relation projection",
                idempotency_key=f"{key}:project",
            )
            requirement, modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=1,
                goal=project.goal,
                hard_constraints=("evidence required",),
                preferences=(),
                available_resources=(),
                unknowns=(),
                modules=(),
                idempotency_key=f"{key}:requirements",
            )
            current = await store.get_project(project.id)
            assert current is not None
            proposal = await app.propose_execution_plan(
                proposal=ExecutionPlanProposal(
                    project_id=project.id,
                    basis_hash=canonical_hash(requirement.id, modules),
                    objective="Create a verified replay snapshot before plan resolution",
                    work_summary=("collect evidence",),
                    allowed_coordination_modes=(CoordinationMode.DECOMPOSE,),
                    max_concurrency=1,
                    max_token_budget=4_000,
                ),
                expected_project_revision=current.revision,
                idempotency_key=f"{key}:propose",
            )
            await session.commit()

        async with factory() as session:
            service = ExecutionPlanShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
            )
            snapshot = await service.create_verified_snapshot(
                project_id=project.id,
                execution_plan_id=proposal.id,
            )
            duplicate = await service.create_verified_snapshot(
                project_id=project.id,
                execution_plan_id=proposal.id,
            )
            await session.commit()

            assert snapshot.aggregate_version == 1
            assert duplicate == snapshot

        async with factory() as session:
            store = PostgresDomainStore(session)
            app = ProjectApplication(store)
            current = await store.get_project(project.id)
            assert current is not None
            await app.resolve_execution_plan(
                proposal_id=proposal.id,
                decision=ExecutionPlanStatus.APPROVED,
                scope_hash=proposal.scope_hash or "",
                expected_project_revision=current.revision,
                idempotency_key=f"{key}:resolve",
            )
            await session.commit()

        async with factory() as session:
            service = ExecutionPlanShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
            )
            verification = await service.verify(
                project_id=project.id,
                execution_plan_id=proposal.id,
            )

            assert verification.snapshot == snapshot
            assert verification.snapshot_matches_full_replay
            assert verification.snapshot_tail_hash == verification.full_replay_hash
            assert verification.event_count == 2
            assert verification.tail_event_count == 1
    finally:
        await engine.dispose()
