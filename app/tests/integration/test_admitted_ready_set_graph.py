from __future__ import annotations

import asyncio
import os
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import AsyncConnection, sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.application.service import ProjectApplication
from aidison.application.single_task_research import AgentRunPaused
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.admitted_ready_set_graph import (
    ReadyTaskExecutor,
    build_admitted_ready_set_graph,
)
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ResearchResultStatus,
    ResultEnvelope,
    TaskEnvelope,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunKind
from aidison.runtime.checkpointing import (
    CheckpointRuntime,
    CheckpointSettings,
    normalize_psycopg_url,
)
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _run(project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"ready-graph-run-{uuid4()}",
        basis_hash=_hash("ready-graph-basis"),
        basis_project_revision=1,
        runtime_binding=RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="research",
            graph_revision="research-v2",
            state_schema_version="research-state-v1",
            profile_binding_ref="profile://research/1",
            policy_binding_ref="policy://research/1",
        ),
        thread_id=f"ready-graph-run-{uuid4()}",
    )


def _task(
    *, run: AgentRun, key: str, dependencies: tuple[UUID, ...] = ()
) -> TaskEnvelope:
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


class _AcceptingExecutor(ReadyTaskExecutor):
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        run: AgentRun,
    ) -> None:
        self._session_factory = session_factory
        self._run = run
        self.executed: list[UUID] = []
        self._lock = asyncio.Lock()
        self.active = 0
        self.max_active = 0

    async def execute(self, *, task: TaskEnvelope) -> None:
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            async with self._session_factory() as session:
                result = ResultEnvelope(
                    run_id=self._run.id,
                    task_id=task.id,
                    basis_hash=self._run.basis_hash,
                    producer_attempt_id=uuid4(),
                    producer_generation=1,
                    producer_profile_ref="profile://research/1",
                    status=ResearchResultStatus.SUCCEEDED,
                    artifact_ref="artifact+sha256://" + _hash(task.task_key) + f"/{uuid4()}",
                    manifest_hash=_hash(f"manifest-{task.task_key}"),
                    evidence_refs=(),
                    coverage_observation_refs=(),
                    unresolved_refs=(),
                )
                store = AgentResultStore(session)
                await store.record_result(result)
                await store.admit(
                    AdmissionRecord(
                        run_id=self._run.id,
                        result_id=result.id,
                        result_manifest_hash=result.manifest_hash,
                        disposition=AdmissionDisposition.ACCEPTED,
                        reason_codes=("runtime_fenced", "schema_valid"),
                        admitted_ref=f"admitted://result/{result.id}",
                    )
                )
                await session.commit()
            self.executed.append(task.id)
        finally:
            async with self._lock:
                self.active -= 1


class _FlakyAcceptingExecutor(_AcceptingExecutor):
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], run: AgentRun) -> None:
        super().__init__(session_factory=session_factory, run=run)
        self._failed_once = False

    async def execute(self, *, task: TaskEnvelope) -> None:
        if task.task_key == "retry" and not self._failed_once:
            self._failed_once = True
            await asyncio.sleep(0.06)
            raise RuntimeError("planned leaf failure")
        await super().execute(task=task)


class _PauseAfterStartedPeerExecutor(_AcceptingExecutor):
    """One leaf pauses while an already-dispatched peer settles normally."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], run: AgentRun) -> None:
        super().__init__(session_factory=session_factory, run=run)
        self._slow_started = asyncio.Event()
        self._release_slow = asyncio.Event()
        self.slow_completed = asyncio.Event()

    async def execute(self, *, task: TaskEnvelope) -> None:
        if task.task_key == "pause":
            await self._slow_started.wait()
            self._release_slow.set()
            raise AgentRunPaused("pause acknowledged before this leaf dispatched")
        if task.task_key == "slow":
            self._slow_started.set()
            await self._release_slow.wait()
            await asyncio.sleep(0.05)
            await super().execute(task=task)
            self.slow_completed.set()
            return
        raise AssertionError(f"unexpected task dispatched after pause: {task.task_key}")


class _TimelineAcceptingExecutor(_AcceptingExecutor):
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        run: AgentRun,
        delays: dict[str, float],
    ) -> None:
        super().__init__(session_factory=session_factory, run=run)
        self._delays = delays
        self.started_at: dict[str, float] = {}
        self.finished_at: dict[str, float] = {}

    async def execute(self, *, task: TaskEnvelope) -> None:
        self.started_at[task.task_key] = asyncio.get_running_loop().time()
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self._delays[task.task_key])
            async with self._session_factory() as session:
                result = ResultEnvelope(
                    run_id=self._run.id,
                    task_id=task.id,
                    basis_hash=self._run.basis_hash,
                    producer_attempt_id=uuid4(),
                    producer_generation=1,
                    producer_profile_ref="profile://research/1",
                    status=ResearchResultStatus.SUCCEEDED,
                    artifact_ref="artifact+sha256://" + _hash(task.task_key) + f"/{uuid4()}",
                    manifest_hash=_hash(f"manifest-{task.task_key}"),
                    evidence_refs=(),
                    coverage_observation_refs=(),
                    unresolved_refs=(),
                )
                store = AgentResultStore(session)
                await store.record_result(result)
                await store.admit(
                    AdmissionRecord(
                        run_id=self._run.id,
                        result_id=result.id,
                        result_manifest_hash=result.manifest_hash,
                        disposition=AdmissionDisposition.ACCEPTED,
                        reason_codes=("runtime_fenced", "schema_valid"),
                        admitted_ref=f"admitted://result/{result.id}",
                    )
                )
                await session.commit()
            self.finished_at[task.task_key] = asyncio.get_running_loop().time()
            self.executed.append(task.id)
        finally:
            async with self._lock:
                self.active -= 1


@pytest.mark.asyncio
async def test_graph_runs_ready_tasks_then_unlocks_dependency_from_durable_admission() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    schema_name = f"aidison_ready_set_graph_{uuid4().hex}"
    checkpoint_runtime = CheckpointRuntime(
        CheckpointSettings(
            database_url=database_url,
            schema_name=schema_name,
            min_pool_size=1,
            max_pool_size=1,
        )
    )
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Ready graph fixture",
                goal="Run child only after parent is admitted",
                idempotency_key=f"ready-graph-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            await session.commit()

        parent = _task(run=run, key="parent")
        child = _task(run=run, key="child", dependencies=(parent.id,))
        executor = _AcceptingExecutor(session_factory=factory, run=run)
        saver = await checkpoint_runtime.start()
        graph = build_admitted_ready_set_graph(
            checkpointer=saver,
            session_factory=factory,
            executor=executor,
        )
        graph_config: RunnableConfig = {"configurable": {"thread_id": str(run.id)}}
        state = await graph.ainvoke(
            {
                "tasks": tuple(item.model_dump(mode="json") for item in (child, parent)),
                "available_capacity": 2,
            },
            graph_config,
        )

        assert executor.executed == [parent.id, child.id]
        assert state["terminal_outcome"] == "complete"
        assert {UUID(item) for item in state["admitted_task_ids"]} == {parent.id, child.id}
        assert state["blocked_task_ids"] == ()
        assert await saver.aget_tuple(graph_config) is not None
    finally:
        await checkpoint_runtime.close()
        async with await AsyncConnection.connect(
            normalize_psycopg_url(database_url), autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_graph_uses_langgraph_tasks_for_same_wave_parallelism() -> None:
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
                name="Ready graph parallel fixture",
                goal="Run independent tasks in one bounded durable wave",
                idempotency_key=f"ready-graph-parallel-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            await session.commit()

        first = _task(run=run, key="first")
        second = _task(run=run, key="second")
        executor = _AcceptingExecutor(session_factory=factory, run=run)
        graph = build_admitted_ready_set_graph(
            checkpointer=InMemorySaver(),
            session_factory=factory,
            executor=executor,
        )
        state = await graph.ainvoke(
            {
                "tasks": tuple(item.model_dump(mode="json") for item in (first, second)),
                "available_capacity": 2,
            },
            {"configurable": {"thread_id": str(run.id)}},
        )

        assert state["terminal_outcome"] == "complete"
        assert executor.max_active == 2
        assert set(executor.executed) == {first.id, second.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admission_of_fast_task_unlocks_dependency_before_slow_peer_finishes() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    schema_name = f"aidison_ready_set_completion_{uuid4().hex}"
    checkpoint_runtime = CheckpointRuntime(
        CheckpointSettings(
            database_url=database_url,
            schema_name=schema_name,
            min_pool_size=1,
            max_pool_size=1,
        )
    )
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Ready graph completion fixture",
                goal="Start dependent work as soon as its prerequisite is admitted",
                idempotency_key=f"ready-graph-completion-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            await session.commit()

        fast = _task(run=run, key="fast")
        slow = _task(run=run, key="slow")
        dependent = _task(run=run, key="dependent", dependencies=(fast.id,))
        executor = _TimelineAcceptingExecutor(
            session_factory=factory,
            run=run,
            delays={"fast": 0.01, "slow": 0.20, "dependent": 0.01},
        )
        graph = build_admitted_ready_set_graph(
            checkpointer=await checkpoint_runtime.start(),
            session_factory=factory,
            executor=executor,
        )

        state = await graph.ainvoke(
            {
                "tasks": tuple(
                    item.model_dump(mode="json") for item in (fast, slow, dependent)
                ),
                "available_capacity": 2,
            },
            {"configurable": {"thread_id": str(run.id)}},
        )

        assert state["terminal_outcome"] == "complete"
        assert executor.started_at["dependent"] < executor.finished_at["slow"]
    finally:
        await checkpoint_runtime.close()
        async with await AsyncConnection.connect(
            normalize_psycopg_url(database_url), autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_checkpoint_resume_reuses_completed_leaf_task_after_peer_failure() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    schema_name = f"aidison_ready_set_resume_{uuid4().hex}"
    checkpoint_runtime = CheckpointRuntime(
        CheckpointSettings(
            database_url=database_url,
            schema_name=schema_name,
            min_pool_size=1,
            max_pool_size=1,
        )
    )
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_results CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Ready graph recovery fixture",
                goal="Do not rerun a completed task after its peer fails",
                idempotency_key=f"ready-graph-recovery-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            await session.commit()

        fast = _task(run=run, key="fast")
        retry = _task(run=run, key="retry")
        executor = _FlakyAcceptingExecutor(session_factory=factory, run=run)
        graph = build_admitted_ready_set_graph(
            checkpointer=await checkpoint_runtime.start(),
            session_factory=factory,
            executor=executor,
        )
        config: RunnableConfig = {"configurable": {"thread_id": str(run.id)}}
        with pytest.raises(RuntimeError, match="planned leaf failure"):
            await graph.ainvoke(
                {
                    "tasks": tuple(item.model_dump(mode="json") for item in (fast, retry)),
                    "available_capacity": 2,
                },
                config,
            )

        resumed = await graph.ainvoke(None, config)
        assert resumed["terminal_outcome"] == "complete"
        assert executor.executed.count(fast.id) == 1
        assert executor.executed.count(retry.id) == 1
    finally:
        await checkpoint_runtime.close()
        async with await AsyncConnection.connect(
            normalize_psycopg_url(database_url), autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_graph_ignores_accepted_result_from_another_task_graph_revision() -> None:
    """A gap/verifier admission must not make an initial graph look partial.

    The scheduler only receives one immutable task graph at a time, while the
    durable Run ledger is intentionally broader and retains every accepted
    result.  The terminal check must use the same graph-local subset as
    dependency resolution.
    """

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
                name="Ready graph foreign admission fixture",
                goal="Keep task graph completion scoped to its own immutable leaves",
                idempotency_key=f"ready-graph-foreign-admission-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            await session.commit()

        first = _task(run=run, key="first")
        second = _task(run=run, key="second")
        gap_task = _task(run=run, key="gap-revision-two")
        executor = _AcceptingExecutor(session_factory=factory, run=run)
        await executor.execute(task=gap_task)
        graph = build_admitted_ready_set_graph(
            checkpointer=InMemorySaver(),
            session_factory=factory,
            executor=executor,
        )

        state = await graph.ainvoke(
            {
                "tasks": tuple(item.model_dump(mode="json") for item in (first, second)),
                "available_capacity": 2,
            },
            {"configurable": {"thread_id": str(run.id)}},
        )

        assert state["terminal_outcome"] == "complete"
        assert {UUID(item) for item in state["admitted_task_ids"]} == {first.id, second.id}
        assert executor.executed.count(gap_task.id) == 1
        assert set(executor.executed) == {gap_task.id, first.id, second.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pause_drains_in_flight_leaf_without_dispatching_more_work() -> None:
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
                name="Ready graph pause drain fixture",
                goal="Drain safely dispatched work before a multi-task pause returns",
                idempotency_key=f"ready-graph-pause-drain-project-{uuid4()}",
            )
            run = _run(project.id)
            await AgentRunControl(session).create(run)
            await session.commit()

        pause = _task(run=run, key="pause")
        slow = _task(run=run, key="slow")
        deferred = _task(run=run, key="z-deferred")
        executor = _PauseAfterStartedPeerExecutor(session_factory=factory, run=run)
        graph = build_admitted_ready_set_graph(
            checkpointer=InMemorySaver(),
            session_factory=factory,
            executor=executor,
        )

        with pytest.raises(AgentRunPaused, match="pause acknowledged"):
            await graph.ainvoke(
                {
                    "tasks": tuple(
                        item.model_dump(mode="json") for item in (pause, slow, deferred)
                    ),
                    "available_capacity": 2,
                },
                {"configurable": {"thread_id": str(run.id)}},
            )

        assert executor.slow_completed.is_set()
        assert executor.executed == [slow.id]
    finally:
        await engine.dispose()
