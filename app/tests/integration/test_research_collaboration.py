from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.orm import (
    AgentRunResultRow,
    ProjectRow,
    ResearchCollaborationObservationRow,
)
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.collaboration import ResearchCollaborationService, ResearchDiscoveryLead
from aidison.research.langgraph_contracts import TaskEnvelope
from aidison.runtime.agent_runs import AgentRun, AgentRunKind
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _run(project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"collaboration-run-{uuid4()}",
        basis_hash=_hash("collaboration-basis"),
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
        thread_id=f"collaboration-run-{uuid4()}",
    )


def _task(*, run: AgentRun, key: str) -> TaskEnvelope:
    return TaskEnvelope(
        run_id=run.id,
        task_key=key,
        basis_hash=run.basis_hash,
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=(),
        coverage_keys=(f"coverage.{key}",),
        allowed_tool_ids=(),
        budget_ref=f"budget://{run.id}",
        idempotency_key=f"task:{run.id}:{key}",
    )


@pytest.mark.asyncio
async def test_persisted_peer_source_discovery_survives_a_new_subscription(
    tmp_path: Path,
) -> None:
    """A live hint is optional; PostgreSQL + Artifact are the recovery authority."""

    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(
                text("TRUNCATE TABLE research_collaboration_observations CASCADE")
            )
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Research collaboration fixture",
                goal="Verify durable peer source discovery does not become project fact",
                idempotency_key=f"collaboration-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            await session.commit()

        producer = _task(run=run, key="research.power-search")
        consumer = _task(run=run, key="research.motor-search")
        lead = ResearchDiscoveryLead(
            canonical_locator="https://example.test/motor-esc-compatible",
            source_kind="web",
            source_origin="web-origin:https://example.test",
            snapshot_ref="artifact+sha256://" + _hash("snapshot") + f"/{uuid4()}",
        )
        service = ResearchCollaborationService(
            session_factory=factory,
            artifact_root=tmp_path / "artifacts",
        )
        async with service.subscribe(run=run, task=consumer) as consumer_session:
            async with service.subscribe(run=run, task=producer) as producer_session:
                await producer_session.publish_source_discovery(
                    source_collection_report_ref=(
                        "artifact+sha256://" + _hash("source-report") + f"/{uuid4()}"
                    ),
                    leads=(lead,),
                )
            live_context = await consumer_session.load_context()

        assert "motor-esc-compatible" in live_context
        assert "non-evidence" in live_context

        # A fresh service has no in-memory queue. It must still recover the
        # committed message from the durable cursor and immutable artifact.
        recovered_service = ResearchCollaborationService(
            session_factory=factory,
            artifact_root=tmp_path / "artifacts",
        )
        async with recovered_service.subscribe(run=run, task=consumer) as recovered_session:
            recovered_context = await recovered_session.load_context()
        assert recovered_context == live_context

        async with factory() as session:
            observation_count = await session.scalar(
                select(func.count()).select_from(ResearchCollaborationObservationRow)
            )
            result_count = await session.scalar(select(func.count()).select_from(AgentRunResultRow))
            stored_project = await session.get(ProjectRow, project.id)
        assert observation_count == 1
        assert result_count == 0
        assert stored_project is not None
        assert stored_project.revision == project.revision
    finally:
        await engine.dispose()
