"""Artifact writes must not break a caller-owned AgentRun transaction."""

from __future__ import annotations

import os
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.orm import ArtifactRow
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_runs import AgentRun, AgentRunKind
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _run(project_id):
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"artifact-transaction-{uuid4()}",
        basis_hash=_hash("artifact-transaction-basis"),
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
        thread_id=f"artifact-transaction-{uuid4()}",
    )


@pytest.mark.asyncio
async def test_non_committing_artifact_write_stays_in_callers_transaction(tmp_path) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE artifacts CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Artifact transaction fixture",
                goal="Verify artifact writes do not commit a Run transaction prematurely",
                idempotency_key=f"artifact-project-{uuid4()}",
            )
            run = await AgentRunControl(session).create(_run(project.id))
            await session.commit()

        async with factory() as writer:
            artifact_store = ContentAddressedArtifactStore(writer, tmp_path)
            artifact = await artifact_store.put_agent_run_json(
                project_id=project.id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="transaction-boundary",
                value={"step": "prepared"},
                commit=False,
            )

            async with factory() as reader:
                before_commit = await reader.scalar(
                    select(func.count())
                    .select_from(ArtifactRow)
                    .where(ArtifactRow.id == artifact.id)
                )
                assert before_commit == 0

            await writer.commit()

        async with factory() as reader:
            after_commit = await reader.scalar(
                select(func.count())
                .select_from(ArtifactRow)
                .where(ArtifactRow.id == artifact.id)
            )
            assert after_commit == 1
    finally:
        await engine.dispose()
