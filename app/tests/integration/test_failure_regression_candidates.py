from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.api.app import create_app
from aidison.application.failure_regression import (
    FAILURE_REGRESSION_CANDIDATE_ARTIFACT_KIND,
)
from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_run_events import AgentRunEventType
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _binding() -> RuntimeBinding:
    return RuntimeBinding(
        runtime_family=RuntimeFamily.LANGGRAPH_V1,
        runtime_revision="runtime-v1",
        graph_key="research",
        graph_revision="research-r7",
        state_schema_version="research-state-v2",
        profile_binding_ref="profile://research/default",
        policy_binding_ref="policy://research/default",
    )


async def _failed_run(
    session: AsyncSession,
    *,
    project_id: UUID,
    revision: int,
) -> AgentRun:
    control = AgentRunControl(session)
    run = await control.create(
        AgentRun(
            project_id=project_id,
            kind=AgentRunKind.RESEARCH,
            idempotency_key=f"failure-regression-run-{uuid4()}",
            basis_hash=sha256(f"failure-basis-{uuid4()}".encode()).hexdigest(),
            basis_project_revision=revision,
            runtime_binding=_binding(),
            thread_id=f"failure-regression-thread-{uuid4()}",
        )
    )
    await control.record_queued_event(
        run_id=run.id,
        event_type=AgentRunEventType.RESEARCH_QUEUED,
        context={},
        artifact_refs=(),
    )
    claim = await control.claim_next(
        worker_id="failure-regression-worker",
        lease_seconds=60,
        run_id=run.id,
    )
    assert claim is not None
    return await control.complete(claim=claim, status=AgentRunStatus.FAILED)


@pytest.mark.asyncio
async def test_failed_run_capture_is_idempotent_and_cannot_claim_golden_status(
    tmp_path: Path,
) -> None:
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
                name="Failure regression fixture",
                goal="Freeze a failed AgentRun for human evaluation review",
                idempotency_key=f"failure-regression-project-{uuid4()}",
            )
            failed = await _failed_run(
                session,
                project_id=project.id,
                revision=project.revision,
            )
            queued = await AgentRunControl(session).create(
                AgentRun(
                    project_id=project.id,
                    kind=AgentRunKind.RESEARCH,
                    idempotency_key=f"nonfailed-run-{uuid4()}",
                    basis_hash=sha256(b"nonfailed-basis").hexdigest(),
                    basis_project_revision=project.revision,
                    runtime_binding=_binding(),
                    thread_id=f"nonfailed-thread-{uuid4()}",
                )
            )
            await session.commit()

        api = create_app(factory, artifact_root=tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://test",
        ) as client:
            endpoint = (
                f"/api/projects/{project.id}/agent-runs/{failed.id}/evaluation-candidates"
            )
            capture_key = f"capture-failure-{uuid4()}"
            deduplicate_key = f"capture-failure-{uuid4()}"
            first = await client.post(endpoint, headers={"Idempotency-Key": capture_key})
            assert first.status_code == 201
            payload = first.json()
            candidate = payload["candidate"]
            assert candidate["review_state"] == "pending_human_label"
            assert candidate["requires_human_review"] is True
            assert candidate["eligible_for_golden_promotion"] is False
            assert candidate["replay_bundle_status"] == "replay_incomplete"
            assert candidate["replay_bundle_reason_codes"] == ["missing_admitted_checkpoint"]
            assert candidate["promotion_blockers"] == [
                "human_expected_outcome_required",
                "human_scoring_rubric_required",
                "replay_missing_admitted_checkpoint",
            ]
            assert candidate["failure_analysis"]["terminal_failure_recorded"] is True

            replayed = await client.post(
                endpoint,
                headers={"Idempotency-Key": capture_key},
            )
            assert replayed.status_code == 201
            assert replayed.json() == payload

            deduplicated = await client.post(
                endpoint,
                headers={"Idempotency-Key": deduplicate_key},
            )
            assert deduplicated.status_code == 201
            assert deduplicated.json() == payload

            listed = await client.get(endpoint)
            assert listed.status_code == 200
            assert listed.json() == [payload]

            rejected = await client.post(
                f"/api/projects/{project.id}/agent-runs/{queued.id}/evaluation-candidates",
                headers={"Idempotency-Key": f"capture-nonfailed-{uuid4()}"},
            )
            assert rejected.status_code == 409

            rendered = json.dumps(payload)
            for forbidden in (
                "prompt",
                "request_hash",
                "idempotency_key",
                "provider_request_id",
                "raw_response",
            ):
                assert forbidden not in rendered

        async with factory() as session:
            artifacts = ContentAddressedArtifactStore(
                session,
                tmp_path,
                record_integrity_status=False,
            )
            candidates = await artifacts.list_metadata(
                project_id=project.id,
                agent_run_id=failed.id,
                kind=FAILURE_REGRESSION_CANDIDATE_ARTIFACT_KIND,
            )
            replay_bundles = await artifacts.list_metadata(
                project_id=project.id,
                agent_run_id=failed.id,
                kind="evaluation_replay_bundle",
            )
            events = await PostgresDomainStore(session).list_events(project.id, limit=200)
            candidate_events = [
                event
                for event in events
                if event.event_type == "evaluation.failure_candidate_created"
            ]
            assert len(candidates) == 1
            assert len(replay_bundles) == 1
            assert len(candidate_events) == 1
    finally:
        await engine.dispose()
