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
from aidison.runtime.agent_runs import (
    AdmittedCheckpointRef,
    AgentRun,
    AgentRunKind,
    AgentRunStatus,
    execution_checkpoint_thread_id,
)
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
    replayable: bool = False,
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
    if replayable:
        await control.admit_checkpoint(
            claim=claim,
            checkpoint=AdmittedCheckpointRef(
                thread_id=execution_checkpoint_thread_id(
                    logical_thread_id=run.thread_id,
                    generation=claim.generation,
                ),
                checkpoint_id="failure-regression-checkpoint",
                graph_revision=run.runtime_binding.graph_revision,
                state_schema_version=run.runtime_binding.state_schema_version,
                generation=claim.generation,
            ),
        )
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

            blocked_promotion = await client.post(
                f"{endpoint}/{candidate['candidate_key']}/review",
                headers={"Idempotency-Key": f"review-incomplete-{uuid4()}"},
                json={
                    "decision": "approved",
                    "reviewed_by": "evaluation-owner",
                    "review_notes": "Replay input is incomplete.",
                    "expected_outcome": "The run should recover.",
                    "oracle": {
                        "expected_terminal_status": "succeeded",
                        "max_provider_attempts": 3,
                        "max_consumed_tokens": 4000,
                    },
                },
            )
            assert blocked_promotion.status_code == 409

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


@pytest.mark.asyncio
async def test_human_review_promotes_replayable_failure_and_scores_a_later_run(
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
                name="Golden failure regression fixture",
                goal="Promote one reviewed failure and score a later AgentRun",
                idempotency_key=f"golden-regression-project-{uuid4()}",
            )
            source = await _failed_run(
                session,
                project_id=project.id,
                revision=project.revision,
                replayable=True,
            )
            control = AgentRunControl(session)
            observed = await control.create(
                AgentRun(
                    project_id=project.id,
                    kind=AgentRunKind.RESEARCH,
                    idempotency_key=f"golden-observed-run-{uuid4()}",
                    basis_hash=sha256(b"golden-observed-basis").hexdigest(),
                    basis_project_revision=project.revision,
                    runtime_binding=_binding(),
                    thread_id=f"golden-observed-thread-{uuid4()}",
                )
            )
            await control.record_queued_event(
                run_id=observed.id,
                event_type=AgentRunEventType.RESEARCH_QUEUED,
                context={},
                artifact_refs=(),
            )
            observed_claim = await control.claim_next(
                worker_id="golden-observed-worker",
                lease_seconds=60,
                run_id=observed.id,
            )
            assert observed_claim is not None
            observed = await control.complete(
                claim=observed_claim,
                status=AgentRunStatus.SUCCEEDED,
            )
            await session.commit()

        api = create_app(factory, artifact_root=tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://test",
        ) as client:
            candidate_endpoint = (
                f"/api/projects/{project.id}/agent-runs/{source.id}/evaluation-candidates"
            )
            captured = await client.post(
                candidate_endpoint,
                headers={"Idempotency-Key": f"golden-capture-{uuid4()}"},
            )
            assert captured.status_code == 201, captured.text
            candidate = captured.json()["candidate"]
            assert candidate["replay_bundle_status"] == "replayable"

            review = await client.post(
                f"{candidate_endpoint}/{candidate['candidate_key']}/review",
                headers={"Idempotency-Key": f"golden-review-{uuid4()}"},
                json={
                    "decision": "approved",
                    "reviewed_by": "evaluation-owner",
                    "review_notes": "The repaired runtime must finish without retry ambiguity.",
                    "expected_outcome": "Research finishes successfully within the fixed budget.",
                    "oracle": {
                        "expected_terminal_status": "succeeded",
                        "required_event_types": ["agent_run.succeeded"],
                        "forbidden_failure_codes": ["runtime_no_progress"],
                        "max_provider_attempts": 0,
                        "max_consumed_tokens": 0,
                        "max_ambiguous_effects": 0,
                        "require_self_recovery": False,
                    },
                },
            )
            assert review.status_code == 201, review.text
            review_payload = review.json()
            golden = review_payload["golden_task"]
            assert review_payload["review"]["decision"] == "approved"
            assert golden["source_candidate_key"] == candidate["candidate_key"]

            listed = await client.get(
                f"/api/projects/{project.id}/agent-runs/{source.id}/golden-regression-tasks"
            )
            assert listed.status_code == 200, listed.text
            assert listed.json() == [golden]

            evaluated = await client.post(
                f"/api/projects/{project.id}/agent-runs/{source.id}/"
                f"golden-regression-tasks/{golden['golden_task_key']}/evaluate/{observed.id}",
                headers={"Idempotency-Key": f"golden-evaluate-{uuid4()}"},
            )
            assert evaluated.status_code == 201, evaluated.text
            report = evaluated.json()["report"]
            assert report["summary"]["passed"] == 1
            assert report["summary"]["failed"] == 0
            assert report["cases"][0]["metric_id"] == "agent_run_regression_oracle"
    finally:
        await engine.dispose()
