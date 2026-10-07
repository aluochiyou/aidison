from __future__ import annotations

import os
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from aidison.application.admitted_ready_set import AdmittedReadySetApplication
from aidison.application.event_replay import AgentResultShadowProjectionService
from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_results import AgentResultConflictError, AgentResultStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.replay_snapshots import EventReplaySnapshotRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ResearchResultStatus,
    ResultEnvelope,
    TaskEnvelope,
)
from aidison.runtime.agent_result_events import replay_agent_result
from aidison.runtime.agent_runs import AgentRun, AgentRunKind
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _run(project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"result-run-{uuid4()}",
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
        thread_id=f"result-run-{uuid4()}",
    )


def _task(*, run: AgentRun, key: str, dependencies: tuple[UUID, ...] = ()) -> TaskEnvelope:
    return TaskEnvelope(
        run_id=run.id,
        task_key=key,
        basis_hash=run.basis_hash,
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=dependencies,
        coverage_keys=(f"coverage.{key}",),
        allowed_tool_ids=(),
        budget_ref=f"budget://{run.id}",
        idempotency_key=f"task:{run.id}:{key}",
    )


def _result(*, run: AgentRun, task: TaskEnvelope, value: str) -> ResultEnvelope:
    return ResultEnvelope(
        run_id=run.id,
        task_id=task.id,
        basis_hash=run.basis_hash,
        producer_attempt_id=uuid4(),
        producer_generation=1,
        producer_profile_ref="profile://research/1",
        status=ResearchResultStatus.SUCCEEDED,
        artifact_ref="artifact+sha256://" + _hash(f"raw-{value}") + f"/{uuid4()}",
        manifest_hash=_hash(f"manifest-{value}"),
        evidence_refs=(),
        coverage_observation_refs=(),
        unresolved_refs=(),
    )


@pytest.mark.asyncio
async def test_result_and_admission_are_durable_separate_and_manifest_fenced() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Agent result admission fixture",
                goal="Verify a durable result cannot be admitted under another manifest",
                idempotency_key=f"result-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            result = ResultEnvelope(
                run_id=run.id,
                task_id=uuid4(),
                basis_hash=run.basis_hash,
                producer_attempt_id=uuid4(),
                producer_generation=1,
                producer_profile_ref="profile://research/1",
                status=ResearchResultStatus.SUCCEEDED,
                artifact_ref="artifact+sha256://" + _hash("raw") + f"/{uuid4()}",
                manifest_hash=_hash("manifest"),
                evidence_refs=(),
                coverage_observation_refs=(),
                unresolved_refs=(),
            )
            store = AgentResultStore(session)
            assert await store.record_result(result) == result
            admission = AdmissionRecord(
                run_id=run.id,
                result_id=result.id,
                result_manifest_hash=result.manifest_hash,
                disposition=AdmissionDisposition.ACCEPTED,
                reason_codes=("runtime_fenced", "schema_valid"),
                admitted_ref="admitted://result/1",
            )
            assert await store.admit(admission) == admission
            assert await store.admit(admission) == admission
            events = await PostgresDomainStore(session).list_aggregate_events(
                project.id,
                aggregate_type="agent_run_result",
                aggregate_id=result.id,
            )
            assert [event.aggregate_version for event in events] == [1, 2]
            rebuilt = replay_agent_result(list(events))
            assert rebuilt.result_envelope == result
            assert rebuilt.admission_record == admission
            with pytest.raises(AgentResultConflictError, match="manifest"):
                await store.admit(
                    admission.model_copy(update={"result_manifest_hash": _hash("wrong")})
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_result_verified_snapshot_tail_replay_keeps_admission_separate() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

        async with factory() as session:
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Agent result snapshot fixture",
                goal="Verify result admission snapshot and event tail replay",
                idempotency_key=f"result-snapshot-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            task = _task(run=run, key="result-snapshot")
            result = _result(run=run, task=task, value="snapshot")
            results = AgentResultStore(session)
            await results.record_result(result)
            await session.commit()

        async with factory() as session:
            service = AgentResultShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                AgentResultStore(session),
            )
            snapshot = await service.create_verified_snapshot(
                project_id=project.id,
                result_id=result.id,
            )
            duplicate = await service.create_verified_snapshot(
                project_id=project.id,
                result_id=result.id,
            )
            await session.commit()

            assert snapshot.aggregate_version == 1
            assert duplicate == snapshot

        async with factory() as session:
            await AgentResultStore(session).admit(
                AdmissionRecord(
                    run_id=run.id,
                    result_id=result.id,
                    result_manifest_hash=result.manifest_hash,
                    disposition=AdmissionDisposition.ACCEPTED,
                    reason_codes=("runtime_fenced", "schema_valid"),
                    admitted_ref=f"admitted://result/{result.id}",
                )
            )
            await session.commit()

        async with factory() as session:
            verification = await AgentResultShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                AgentResultStore(session),
            ).verify(
                project_id=project.id,
                result_id=result.id,
            )

            assert verification.snapshot == snapshot
            assert verification.snapshot_matches_full_replay
            assert verification.snapshot_tail_hash == verification.full_replay_hash
            assert verification.event_count == 2
            assert verification.tail_event_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_durable_admission_is_the_only_dependency_unlock_signal() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Admitted ready-set fixture",
                goal="Verify rejected result cannot unlock a dependent task",
                idempotency_key=f"ready-set-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            parent = _task(run=run, key="parent")
            child = _task(run=run, key="child", dependencies=(parent.id,))
            ready_set = AdmittedReadySetApplication(session)

            initial = await ready_set.derive(tasks=(child, parent), available_capacity=4)
            assert initial.task_ids == (parent.id,)

            rejected = _result(run=run, task=parent, value="rejected")
            store = AgentResultStore(session)
            await store.record_result(rejected)
            await store.admit(
                AdmissionRecord(
                    run_id=run.id,
                    result_id=rejected.id,
                    result_manifest_hash=rejected.manifest_hash,
                    disposition=AdmissionDisposition.REJECTED,
                    reason_codes=("artifact_missing",),
                )
            )
            still_parent = await ready_set.derive(tasks=(child, parent), available_capacity=4)
            assert still_parent.task_ids == (parent.id,)

            accepted = _result(run=run, task=parent, value="accepted-retry")
            await store.record_result(accepted)
            await store.admit(
                AdmissionRecord(
                    run_id=run.id,
                    result_id=accepted.id,
                    result_manifest_hash=accepted.manifest_hash,
                    disposition=AdmissionDisposition.ACCEPTED,
                    reason_codes=("runtime_fenced", "schema_valid"),
                    admitted_ref=f"admitted://result/{accepted.id}",
                )
            )
            unlocked = await ready_set.derive(tasks=(child, parent), available_capacity=4)
            assert unlocked.task_ids == (child.id,)
    finally:
        await engine.dispose()
