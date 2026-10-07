from __future__ import annotations

import os
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from aidison.application.event_replay import AgentRunDecisionShadowProjectionService
from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_decisions import (
    AgentRunDecisionConflictError,
    AgentRunDecisionStore,
)
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.replay_snapshots import EventReplaySnapshotRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.decision_contracts import AgentRunDecisionStatus
from aidison.research.langgraph_contracts import ProposalManifest
from aidison.runtime.agent_run_decision_events import replay_agent_run_decision
from aidison.runtime.agent_runs import AgentRun, AgentRunKind
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _run(project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"decision-run-{uuid4()}",
        basis_hash=_hash("basis"),
        basis_project_revision=1,
        runtime_binding=RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="research",
            graph_revision="research-v1",
            state_schema_version="research-state-v1",
            profile_binding_ref="profile://research/1",
            policy_binding_ref="policy://research/1",
        ),
        thread_id=f"decision-run-{uuid4()}",
    )


@pytest.mark.asyncio
async def test_proposal_review_is_durable_event_backed_and_single_verdict() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_decisions CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun decision event fixture",
                goal="Verify a proposal review can be replayed without treating it as a solution",
                idempotency_key=f"decision-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            proposal = ProposalManifest(
                run_id=run.id,
                basis_hash=run.basis_hash,
                artifact_ref=f"artifact+sha256://{_hash('proposal')}/manifest.json",
                manifest_hash=_hash("proposal"),
            )
            store = AgentRunDecisionStore(session)
            prepared = await store.prepare(run=run, proposal=proposal)
            resolved = await store.resolve(
                decision_id=prepared.id,
                basis_hash=run.basis_hash,
                answer=AgentRunDecisionStatus.APPROVED,
            )
            assert await store.resolve(
                decision_id=prepared.id,
                basis_hash=run.basis_hash,
                answer=AgentRunDecisionStatus.APPROVED,
            ) == resolved
            with pytest.raises(AgentRunDecisionConflictError, match="already resolved"):
                await store.resolve(
                    decision_id=prepared.id,
                    basis_hash=run.basis_hash,
                    answer=AgentRunDecisionStatus.REJECTED,
                )
            events = await PostgresDomainStore(session).list_aggregate_events(
                project.id,
                aggregate_type="agent_run_decision",
                aggregate_id=prepared.id,
            )
            assert [event.aggregate_version for event in events] == [1, 2]
            assert replay_agent_run_decision(list(events)).agent_run_decision == resolved
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_decision_verified_snapshot_tail_replay_matches_relation() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_decisions CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

        async with factory() as session:
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun decision snapshot fixture",
                goal="Verify proposal verdict snapshot and event tail replay",
                idempotency_key=f"decision-snapshot-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            proposal = ProposalManifest(
                run_id=run.id,
                basis_hash=run.basis_hash,
                artifact_ref=f"artifact+sha256://{_hash('proposal-snapshot')}/manifest.json",
                manifest_hash=_hash("proposal-snapshot"),
            )
            prepared = await AgentRunDecisionStore(session).prepare(run=run, proposal=proposal)
            await session.commit()

        async with factory() as session:
            service = AgentRunDecisionShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                AgentRunDecisionStore(session),
            )
            snapshot = await service.create_verified_snapshot(
                project_id=project.id,
                decision_id=prepared.id,
            )
            duplicate = await service.create_verified_snapshot(
                project_id=project.id,
                decision_id=prepared.id,
            )
            await session.commit()

            assert snapshot.aggregate_version == 1
            assert duplicate == snapshot

        async with factory() as session:
            resolved = await AgentRunDecisionStore(session).resolve(
                decision_id=prepared.id,
                basis_hash=run.basis_hash,
                answer=AgentRunDecisionStatus.APPROVED,
            )
            await session.commit()
            assert resolved.status is AgentRunDecisionStatus.APPROVED

        async with factory() as session:
            verification = await AgentRunDecisionShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                AgentRunDecisionStore(session),
            ).verify(
                project_id=project.id,
                decision_id=prepared.id,
            )

            assert verification.snapshot == snapshot
            assert verification.snapshot_matches_full_replay
            assert verification.snapshot_tail_hash == verification.full_replay_hash
            assert verification.event_count == 2
            assert verification.tail_event_count == 1
    finally:
        await engine.dispose()
