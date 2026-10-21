from __future__ import annotations

import asyncio
import os
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from aidison.application.admitted_ready_set import AdmittedReadySetApplication
from aidison.application.event_replay import (
    AgentResultProjectionRepairService,
    AgentResultShadowProjectionService,
    ProjectionRepairConflictError,
)
from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_results import AgentResultConflictError, AgentResultStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.orm import AgentRunResultAdmissionRow, AgentRunResultRow
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
async def test_result_projection_repair_restores_existing_result_and_admission_pair() -> None:
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
                name="Agent result repair fixture",
                goal="Restore an existing result and verdict without promoting raw output",
                idempotency_key=f"result-repair-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            result = _result(run=run, task=_task(run=run, key="repair"), value="repair")
            admission = AdmissionRecord(
                run_id=run.id,
                result_id=result.id,
                result_manifest_hash=result.manifest_hash,
                disposition=AdmissionDisposition.ACCEPTED,
                reason_codes=("runtime_fenced", "schema_valid"),
                admitted_ref=f"admitted://result/{result.id}",
            )
            store = AgentResultStore(session)
            await store.record_result(result)
            await store.admit(admission)
            await session.commit()

        async with factory() as session:
            result_row = await session.get(AgentRunResultRow, result.id)
            admission_row = await session.scalar(
                select(AgentRunResultAdmissionRow).where(
                    AgentRunResultAdmissionRow.result_id == result.id
                )
            )
            assert result_row is not None and admission_row is not None
            result_row.event_version = 99
            result_row.payload = {**result_row.payload, "status": "failed"}
            admission_row.disposition = AdmissionDisposition.REJECTED.value
            await session.commit()

        async with factory() as session:
            repair = AgentResultProjectionRepairService(session)
            preview = await repair.preview(
                project_id=project.id,
                agent_run_result_id=result.id,
            )
            assert preview.repair_required
            assert preview.current_relation_hash is not None
            dry_run = await repair.apply(
                project_id=project.id,
                agent_run_result_id=result.id,
                expected_current_relation_hash=preview.current_relation_hash,
            )
            assert dry_run.applied is False
            applied = await repair.apply(
                project_id=project.id,
                agent_run_result_id=result.id,
                expected_current_relation_hash=preview.current_relation_hash,
                dry_run=False,
            )
            assert applied.applied is True
            await session.commit()

        async with factory() as session:
            store = AgentResultStore(session)
            events = await PostgresDomainStore(session).list_aggregate_events(
                project.id,
                aggregate_type="agent_run_result",
                aggregate_id=result.id,
            )
            verification = await AgentResultShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                store,
            ).verify(project_id=project.id, result_id=result.id)

            assert await store.get_result(result_id=result.id) == result
            assert await store.get_admission(result_id=result.id) == admission
            assert len(events) == 2
            assert verification.snapshot_matches_full_replay
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_result_projection_repair_refuses_to_create_a_missing_admission_row() -> None:
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
                name="Result admission shape refusal",
                goal="Do not infer an accepted result from a missing verdict row",
                idempotency_key=f"result-repair-shape-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            result = _result(run=run, task=_task(run=run, key="shape"), value="shape")
            admission = AdmissionRecord(
                run_id=run.id,
                result_id=result.id,
                result_manifest_hash=result.manifest_hash,
                disposition=AdmissionDisposition.ACCEPTED,
                reason_codes=("runtime_fenced",),
                admitted_ref=f"admitted://result/{result.id}",
            )
            store = AgentResultStore(session)
            await store.record_result(result)
            await store.admit(admission)
            await session.commit()

        async with factory() as session:
            row = await session.scalar(
                select(AgentRunResultAdmissionRow).where(
                    AgentRunResultAdmissionRow.result_id == result.id
                )
            )
            assert row is not None
            await session.delete(row)
            await session.commit()

        async with factory() as session:
            repair = AgentResultProjectionRepairService(session)
            preview = await repair.preview(
                project_id=project.id,
                agent_run_result_id=result.id,
            )
            assert preview.current_relation_hash is not None
            with pytest.raises(ProjectionRepairConflictError, match="automatic row creation"):
                await repair.apply(
                    project_id=project.id,
                    agent_run_result_id=result.id,
                    expected_current_relation_hash=preview.current_relation_hash,
                    dry_run=False,
                )
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


@pytest.mark.asyncio
async def test_task_cannot_admit_two_distinct_results_after_retry() -> None:
    """Keep retry evidence durable without letting it fork task completion."""

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
                name="Single accepted task result fixture",
                goal="A duplicate dispatch must not fork a task's accepted output",
                idempotency_key=f"single-accepted-task-result-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            task = _task(run=run, key="retry-target")
            first = _result(run=run, task=task, value="first-attempt")
            retried = _result(run=run, task=task, value="second-attempt")
            store = AgentResultStore(session)
            await store.record_result(first)
            await store.record_result(retried)
            await store.admit(
                AdmissionRecord(
                    run_id=run.id,
                    result_id=first.id,
                    result_manifest_hash=first.manifest_hash,
                    disposition=AdmissionDisposition.ACCEPTED,
                    reason_codes=("runtime_fenced", "schema_valid"),
                    admitted_ref=f"admitted://result/{first.id}",
                )
            )

            with pytest.raises(AgentResultConflictError, match="already has an accepted"):
                await store.admit(
                    AdmissionRecord(
                        run_id=run.id,
                        result_id=retried.id,
                        result_manifest_hash=retried.manifest_hash,
                        disposition=AdmissionDisposition.ACCEPTED,
                        reason_codes=("runtime_fenced", "schema_valid"),
                        admitted_ref=f"admitted://result/{retried.id}",
                    )
                )

            assert await store.admitted_task_ids(run_id=run.id) == (task.id,)
            assert tuple(item.id for item in await store.admitted_results(run_id=run.id)) == (
                first.id,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_identical_result_recording_is_idempotent() -> None:
    """A duplicate producer retry must not turn a unique-key race into a 500."""

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
                name="Concurrent result-recording fixture",
                goal="An identical producer retry must safely reuse the first durable result",
                idempotency_key=f"concurrent-result-recording-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            task = _task(run=run, key="concurrent-result-recording")
            result = _result(run=run, task=task, value="same-retry")
            await session.commit()

        async def record() -> ResultEnvelope:
            async with factory() as session:
                value = await AgentResultStore(session).record_result(result)
                await session.commit()
                return value

        left, right = await asyncio.gather(record(), record())
        assert left == result
        assert right == result

        async with factory() as session:
            rows = (
                await session.scalars(
                    select(AgentRunResultRow).where(AgentRunResultRow.id == result.id)
                )
            ).all()
            events = await PostgresDomainStore(session).list_aggregate_events(
                project.id,
                aggregate_type="agent_run_result",
                aggregate_id=result.id,
            )
            assert len(rows) == 1
            assert [event.aggregate_version for event in events] == [1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_duplicate_admissions_serialize_per_task_without_deadlocking() -> None:
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
                name="Concurrent single-task admission fixture",
                goal="Serialize duplicate admission without blocking another task wave",
                idempotency_key=f"concurrent-task-admission-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            task = _task(run=run, key="concurrent-retry-target")
            first = _result(run=run, task=task, value="concurrent-first")
            second = _result(run=run, task=task, value="concurrent-second")
            store = AgentResultStore(session)
            await store.record_result(first)
            await store.record_result(second)
            await session.commit()

        async def admit(result: ResultEnvelope) -> AdmissionRecord:
            async with factory() as session:
                admission = AdmissionRecord(
                    run_id=run.id,
                    result_id=result.id,
                    result_manifest_hash=result.manifest_hash,
                    disposition=AdmissionDisposition.ACCEPTED,
                    reason_codes=("runtime_fenced", "schema_valid"),
                    admitted_ref=f"admitted://result/{result.id}",
                )
                value = await AgentResultStore(session).admit(admission)
                await session.commit()
                return value

        outcomes = await asyncio.gather(
            admit(first),
            admit(second),
            return_exceptions=True,
        )

        assert sum(isinstance(item, AdmissionRecord) for item in outcomes) == 1
        assert sum(isinstance(item, AgentResultConflictError) for item in outcomes) == 1
        async with factory() as session:
            accepted = await AgentResultStore(session).admitted_results(run_id=run.id)
            assert len(accepted) == 1
            assert accepted[0].task_id == task.id
    finally:
        await engine.dispose()
