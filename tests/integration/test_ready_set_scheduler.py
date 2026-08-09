from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.agents.profiles import RESEARCH_ORCHESTRATOR_PROFILE, build_profile_revision
from aidison.application.service import ProjectApplication
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import (
    BudgetAccountRow,
    JobRow,
    PlanHeadRow,
    PlanRevisionRow,
    PlanTaskClaimRow,
    PlanTaskRow,
)
from aidison.infrastructure.planning import PostgresPlanStore
from aidison.infrastructure.profiles import ProfileRepository
from aidison.infrastructure.ready_set import ReadySetScheduler
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import (
    DelegationResult,
    DelegationStatus,
    JobClaim,
    ResultDisposition,
)
from aidison.runtime.planning import (
    OrchestrationPlanRevision,
    SchedulerSkipReason,
    TaskEdge,
    TaskEdgeKind,
    TaskNode,
)

pytestmark = pytest.mark.integration

READY_WORKER_PROFILE = build_profile_revision(
    profile_id="ready-worker",
    revision=1,
    purpose="Deterministic ready-set test worker that never calls a model.",
    prompt_template="Deterministic ready-set test worker. It performs no model call.",
    input_schema_ref="aidison://schemas/ready-worker-input/v1",
    output_schema_ref="aidison://schemas/ready-worker-output/v1",
    allowed_effects=("read",),
    model_capabilities=("structured_output",),
    token_cap=1_000,
    tool_call_cap=1,
    concurrency_cap=8,
    timeout_seconds=60,
)

READY_WORKER_SLOW_PROFILE = build_profile_revision(
    profile_id="ready-worker-slow",
    revision=1,
    purpose="Deterministic ready-set test worker with a low per-profile concurrency cap.",
    prompt_template="Deterministic ready-set test worker. It performs no model call.",
    input_schema_ref="aidison://schemas/ready-worker-input/v1",
    output_schema_ref="aidison://schemas/ready-worker-output/v1",
    allowed_effects=("read",),
    model_capabilities=("structured_output",),
    token_cap=1_000,
    tool_call_cap=1,
    concurrency_cap=2,
    timeout_seconds=60,
)


def _database() -> tuple[str, Any]:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    return database_url, engine


def _node(
    key: str,
    *,
    depth: int,
    profile_id: str = READY_WORKER_PROFILE.profile_id,
    role_key: str = "ready-worker",
) -> TaskNode:
    return TaskNode(
        logical_key=key,
        objective=f"Run {key}",
        mode="ready",
        role_key=role_key,
        profile_id=profile_id,
        profile_revision=1,
        budget_ref=f"budget://job/{key}",
        input_refs=(f"module://{key}",),
        success_criteria=("done",),
        stop_criteria=("stop",),
        depth=depth,
    )


def _edge(from_key: str, to_key: str) -> TaskEdge:
    return TaskEdge(from_key=from_key, to_key=to_key, kind=TaskEdgeKind.DEPENDS_ON)


def _diamond_plan() -> tuple[tuple[TaskNode, ...], tuple[TaskEdge, ...]]:
    nodes = (
        _node("a", depth=0),
        _node("b", depth=1),
        _node("c", depth=1),
        _node("d", depth=2),
    )
    edges = (_edge("a", "b"), _edge("a", "c"), _edge("b", "d"), _edge("c", "d"))
    return nodes, edges


async def _bootstrap(
    factory: async_sessionmaker[AsyncSession],
    *,
    nodes: tuple[TaskNode, ...],
    edges: tuple[TaskEdge, ...] = (),
    profile: Any | None = READY_WORKER_PROFILE,
    role_key: str = "ready-worker",
    token_cap: int = 4_000,
    tool_cap: int = 4,
    suffix: str = "fixture",
) -> tuple[UUID, str, JobClaim]:
    async with factory() as session:
        await session.execute(text("TRUNCATE TABLE jobs CASCADE"))
        await session.commit()
        project = await ProjectApplication(PostgresDomainStore(session)).create_project(
            name=f"Ready-set {suffix}",
            goal=f"Prove ready-set scheduling for {suffix}",
            idempotency_key=f"ready-set-project-{suffix}-{uuid4()}",
        )
        basis_hash = sha256(f"ready-set-basis-{suffix}".encode()).hexdigest()
        runtime = PostgresRuntime(session)
        root_job_id = await runtime.create_job(
            project_id=project.id,
            kind="ready_set",
            basis_hash=basis_hash,
            basis_project_revision=project.revision,
            profile_id=RESEARCH_ORCHESTRATOR_PROFILE.profile_id,
            profile_revision=RESEARCH_ORCHESTRATOR_PROFILE.revision,
            token_budget_cap=token_cap,
            tool_call_budget_cap=tool_cap,
        )
        if profile is not None:
            await ProfileRepository(session).register(profile, activate=True)
            await ProfileRepository(session).bind_revisions(
                root_job_id=root_job_id,
                roles={role_key: (profile.profile_id, profile.revision)},
            )
        claim = await runtime.claim_next_job(worker_id="ready-set-scheduler", lease_seconds=120)
        assert claim is not None
        plan = OrchestrationPlanRevision(
            root_job_id=str(root_job_id),
            revision=1,
            basis_hash=basis_hash,
            reason=f"ready-set {suffix}",
            planner_profile_id=RESEARCH_ORCHESTRATOR_PROFILE.profile_id,
            planner_profile_revision=RESEARCH_ORCHESTRATOR_PROFILE.revision,
            nodes=nodes,
            edges=edges,
        )
        await PostgresPlanStore(session).create_initial(claim=claim, plan=plan)
        return root_job_id, basis_hash, claim


async def _run_child(
    factory: async_sessionmaker[AsyncSession],
    *,
    basis_hash: str,
    status: DelegationStatus = DelegationStatus.SUCCEEDED,
) -> ResultDisposition:
    async with factory() as session:
        runtime = PostgresRuntime(session)
        claim = await runtime.claim_next_job(worker_id="ready-worker-process", lease_seconds=120)
        assert claim is not None
        work = await runtime.load_work_item(claim)
        assert work.delegation is not None
        registered = await runtime.register_result(
            result=DelegationResult(
                delegation_id=work.delegation.delegation_id,
                child_job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                child_claim_generation=claim.claim_generation,
                status=status,
                basis_hash=basis_hash,
                proposal_ref=f"proposal://{work.delegation.shard_key}",
                normalized_error=(
                    None if status is DelegationStatus.SUCCEEDED else "worker_failed"
                ),
            ),
            lease_token=claim.lease_token,
        )
        return registered.disposition


async def _claim_count(
    factory: async_sessionmaker[AsyncSession], *, root_job_id: UUID
) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(PlanTaskClaimRow)
                .where(PlanTaskClaimRow.root_job_id == root_job_id)
            )
            or 0
        )


async def _claim_generations(
    factory: async_sessionmaker[AsyncSession], *, root_job_id: UUID
) -> list[tuple[str, int, str]]:
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(PlanTaskClaimRow)
                .where(PlanTaskClaimRow.root_job_id == root_job_id)
                .order_by(PlanTaskClaimRow.task_id, PlanTaskClaimRow.claim_generation)
            )
        )
        return [(row.logical_key, row.claim_generation, row.status) for row in rows]


@pytest.mark.asyncio
async def test_ready_set_diamond_fan_out_and_fan_in() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        nodes, edges = _diamond_plan()
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=nodes, edges=edges, suffix="diamond"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)

        first = await scheduler.tick(claim)
        assert [item.task_logical_key for item in first.dispatched] == ["a"]
        assert first.active_count == 1
        assert first.complete is False

        assert await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        second = await scheduler.tick(claim)
        assert sorted(item.task_logical_key for item in second.dispatched) == ["b", "c"]
        assert second.active_count == 2

        for _ in range(2):
            assert await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        third = await scheduler.tick(claim)
        assert [item.task_logical_key for item in third.dispatched] == ["d"]
        assert third.active_count == 1

        assert await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        fourth = await scheduler.tick(claim)
        assert fourth.dispatched == ()
        assert fourth.active_count == 0
        assert fourth.ready_remaining == 0
        assert fourth.complete is True
        assert await _claim_count(factory, root_job_id=root_job_id) == 4
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_max_concurrency_gates_and_releases_slots() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        nodes = tuple(_node(key, depth=0) for key in ("a", "b", "c", "d"))
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=nodes, suffix="concurrency"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=2)

        first = await scheduler.tick(claim)
        assert len(first.dispatched) == 2
        assert {item.reason for item in first.skipped} == {SchedulerSkipReason.CONCURRENCY}
        assert first.active_count == 2

        assert await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        second = await scheduler.tick(claim)
        assert len(second.dispatched) == 1
        assert second.active_count == 2

        assert await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        third = await scheduler.tick(claim)
        assert len(third.dispatched) == 1
        assert third.active_count == 2

        assert await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        assert await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        fourth = await scheduler.tick(claim)
        assert fourth.dispatched == ()
        assert fourth.active_count == 0
        assert fourth.complete is True
        assert await _claim_count(factory, root_job_id=root_job_id) == 4
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_racing_schedulers_dispatch_exactly_once() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=(_node("a", depth=0),), suffix="race"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        first, second = await asyncio.gather(
            scheduler.tick(claim), scheduler.tick(claim)
        )
        dispatched = list(first.dispatched) + list(second.dispatched)
        assert [item.task_logical_key for item in dispatched] == ["a"]
        assert await _claim_count(factory, root_job_id=root_job_id) == 1
        async with factory() as session:
            bound = int(
                await session.scalar(
                    select(func.count())
                    .select_from(PlanTaskRow)
                    .where(PlanTaskRow.dispatched_job_id.is_not(None))
                )
                or 0
            )
        assert bound == 1
        # the runner-up observed a safe no-op, never an error
        runner_up = first if first.dispatched == () else second
        assert {item.reason for item in runner_up.skipped} == {
            SchedulerSkipReason.ALREADY_CLAIMED
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_repeated_tick_is_idempotent() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=(_node("a", depth=0),), suffix="idempotent"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        assert len((await scheduler.tick(claim)).dispatched) == 1
        repeated = await scheduler.tick(claim)
        assert repeated.dispatched == ()
        assert repeated.active_count == 1
        assert await _claim_count(factory, root_job_id=root_job_id) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_retries_failed_task_with_new_claim_generation() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=(_node("a", depth=0),), suffix="retry"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        first = await scheduler.tick(claim)
        assert len(first.dispatched) == 1
        first_claim = first.dispatched[0]

        assert (
            await _run_child(factory, basis_hash=basis_hash, status=DelegationStatus.FAILED)
            is ResultDisposition.ELIGIBLE
        )
        assert await scheduler.retry_task(claim=claim, logical_key="a") is True

        second = await scheduler.tick(claim)
        assert len(second.dispatched) == 1
        second_claim = second.dispatched[0]
        assert second_claim.claim_generation == first_claim.claim_generation + 1
        assert second_claim.child_job_id != first_claim.child_job_id
        assert await _claim_generations(factory, root_job_id=root_job_id) == [
            ("a", 1, "superseded"),
            ("a", 2, "dispatched"),
        ]
        assert (
            await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        )
        final = await scheduler.tick(claim)
        assert final.complete is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_child_reclaim_after_lease_expiry_is_safe() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=(_node("a", depth=0),), suffix="reclaim"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        first = await scheduler.tick(claim)
        child_id = first.dispatched[0].child_job_id
        assert child_id is not None

        async with factory() as session:
            runtime = PostgresRuntime(session)
            worker_one = await runtime.claim_next_job(worker_id="worker-one", lease_seconds=120)
        assert worker_one is not None and worker_one.job_id == child_id

        async with factory() as session:
            await session.execute(
                update(JobRow)
                .where(JobRow.id == child_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

        async with factory() as session:
            runtime = PostgresRuntime(session)
            worker_two = await runtime.claim_next_job(worker_id="worker-two", lease_seconds=120)
            assert worker_two is not None and worker_two.job_id == child_id
            assert worker_two.claim_generation == worker_one.claim_generation + 1
            work = await runtime.load_work_item(worker_two)
            assert work.delegation is not None
            late = await runtime.register_result(
                result=DelegationResult(
                    delegation_id=work.delegation.delegation_id,
                    child_job_id=child_id,
                    attempt_id=worker_one.attempt_id,
                    child_claim_generation=worker_one.claim_generation,
                    status=DelegationStatus.SUCCEEDED,
                    basis_hash=basis_hash,
                    proposal_ref="proposal://late-worker",
                ),
                lease_token=worker_one.lease_token,
            )
            assert late.disposition is ResultDisposition.QUARANTINED
            current = await runtime.register_result(
                result=DelegationResult(
                    delegation_id=work.delegation.delegation_id,
                    child_job_id=child_id,
                    attempt_id=worker_two.attempt_id,
                    child_claim_generation=worker_two.claim_generation,
                    status=DelegationStatus.SUCCEEDED,
                    basis_hash=basis_hash,
                    proposal_ref="proposal://current-worker",
                ),
                lease_token=worker_two.lease_token,
            )
            assert current.disposition is ResultDisposition.ELIGIBLE

        final = await scheduler.tick(claim)
        assert final.complete is True
        assert await _claim_count(factory, root_job_id=root_job_id) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_budget_eligibility_gates_and_releases() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        nodes = tuple(_node(key, depth=0) for key in ("a", "b"))
        root_job_id, basis_hash, claim = await _bootstrap(
            factory,
            nodes=nodes,
            token_cap=READY_WORKER_PROFILE.token_cap,
            tool_cap=READY_WORKER_PROFILE.tool_call_cap,
            suffix="budget",
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        first = await scheduler.tick(claim)
        assert len(first.dispatched) == 1
        assert [item.reason for item in first.skipped] == [SchedulerSkipReason.BUDGET]

        assert (
            await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        )
        second = await scheduler.tick(claim)
        assert len(second.dispatched) == 1
        assert await _claim_count(factory, root_job_id=root_job_id) == 2
        assert await _run_child(factory, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        assert (await scheduler.tick(claim)).complete is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_skips_unbound_role_until_binding_exists() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, basis_hash, claim = await _bootstrap(
            factory,
            nodes=(_node("a", depth=0, role_key="unbound-worker"),),
            profile=None,
            suffix="capability",
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        first = await scheduler.tick(claim)
        assert first.dispatched == ()
        assert [item.reason for item in first.skipped] == [SchedulerSkipReason.CAPABILITY]

        async with factory() as session:
            await ProfileRepository(session).register(READY_WORKER_PROFILE, activate=True)
            await ProfileRepository(session).bind_revisions(
                root_job_id=root_job_id,
                roles={
                    "unbound-worker": (
                        READY_WORKER_PROFILE.profile_id,
                        READY_WORKER_PROFILE.revision,
                    )
                },
            )
            await session.commit()
        second = await scheduler.tick(claim)
        assert len(second.dispatched) == 1
        assert await _claim_count(factory, root_job_id=root_job_id) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_recovers_after_restart_without_duplicate_dispatch() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine_a = create_engine(DatabaseSettings(database_url=database_url))
    factory_a = create_session_factory(engine_a)
    try:
        nodes, edges = _diamond_plan()
        root_job_id, basis_hash, claim = await _bootstrap(
            factory_a, nodes=nodes, edges=edges, suffix="restart"
        )
        first = await ReadySetScheduler(session_factory=factory_a, default_concurrency=8).tick(
            claim
        )
        assert len(first.dispatched) == 1
    finally:
        await engine_a.dispose()

    engine_b = create_engine(DatabaseSettings(database_url=database_url))
    factory_b = create_session_factory(engine_b)
    try:
        scheduler = ReadySetScheduler(session_factory=factory_b, default_concurrency=8)
        resumed = await scheduler.tick(claim)
        assert resumed.dispatched == ()
        assert resumed.active_count == 1
        assert await _claim_count(factory_b, root_job_id=root_job_id) == 1

        assert await _run_child(factory_b, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        second = await scheduler.tick(claim)
        assert sorted(item.task_logical_key for item in second.dispatched) == ["b", "c"]

        for _ in range(2):
            assert await _run_child(factory_b, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        third = await scheduler.tick(claim)
        assert [item.task_logical_key for item in third.dispatched] == ["d"]

        assert await _run_child(factory_b, basis_hash=basis_hash) is ResultDisposition.ELIGIBLE
        assert (await scheduler.tick(claim)).complete is True
    finally:
        await engine_b.dispose()


@pytest.mark.asyncio
async def test_ready_set_expired_claim_recovery_redispatch_once() -> None:
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=(_node("a", depth=0),), suffix="expired-claim"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        # Simulate a scheduler that persisted a claim but never dispatched it.
        async with factory() as session:
            head_row = await session.scalar(
                select(PlanHeadRow).where(PlanHeadRow.root_job_id == root_job_id)
            )
            assert head_row is not None
            revision_row = await session.scalar(
                select(PlanRevisionRow).where(
                    PlanRevisionRow.root_job_id == root_job_id,
                    PlanRevisionRow.revision == head_row.current_revision,
                )
            )
            assert revision_row is not None
            task_row = await session.scalar(
                select(PlanTaskRow).where(
                    PlanTaskRow.plan_revision_id == revision_row.id,
                    PlanTaskRow.logical_key == "a",
                )
            )
            assert task_row is not None

            session.add(
                PlanTaskClaimRow(
                    id=uuid4(),
                    root_job_id=root_job_id,
                    plan_revision_id=revision_row.id,
                    plan_revision=head_row.current_revision,
                    task_id=task_row.id,
                    logical_key="a",
                    claim_generation=1,
                    lease_owner="ghost-scheduler",
                    lease_token=uuid4(),
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=30),
                    status="claimed",
                    intent={
                        "root_job_id": str(root_job_id),
                        "plan_revision": head_row.current_revision,
                        "task_logical_key": "a",
                        "claim_generation": 1,
                        "graph_step_id": "ready:a:claim1",
                        "task_kind": "ready",
                        "role_key": "ready-worker",
                        "profile_id": READY_WORKER_PROFILE.profile_id,
                        "profile_revision": READY_WORKER_PROFILE.revision,
                        "basis_hash": basis_hash,
                        "shard_key": "a",
                        "input_refs": ["module://a"],
                        "token_budget": READY_WORKER_PROFILE.token_cap,
                        "tool_call_budget": READY_WORKER_PROFILE.tool_call_cap,
                        "deadline": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    },
                )
            )
            await session.commit()

        assert await scheduler.recover_expired_claims(claim=claim) == 1
        second = await scheduler.tick(claim)
        assert len(second.dispatched) == 1
        assert await _claim_count(factory, root_job_id=root_job_id) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_expired_queued_child_is_cancelled_and_redispatched() -> None:
    """F4: a child that never leaves QUEUED cannot hold a task forever."""
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, _, claim = await _bootstrap(
            factory, nodes=(_node("a", depth=0),), suffix="queued-child-expiry"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        first = await scheduler.tick(claim)
        assert len(first.dispatched) == 1
        original_child_id = first.dispatched[0].child_job_id
        assert original_child_id is not None

        async with factory() as session:
            await session.execute(
                update(PlanTaskClaimRow)
                .where(
                    PlanTaskClaimRow.root_job_id == root_job_id,
                    PlanTaskClaimRow.child_job_id == original_child_id,
                )
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

        second = await scheduler.tick(claim)
        assert len(second.dispatched) == 1
        assert second.dispatched[0].child_job_id != original_child_id

        async with factory() as session:
            original = await session.get(JobRow, original_child_id)
            assert original is not None
            assert original.status == "cancelled"
            assert original.cancel_requested is True
            statuses = list(
                await session.scalars(
                    select(PlanTaskClaimRow.status)
                    .where(PlanTaskClaimRow.root_job_id == root_job_id)
                    .order_by(PlanTaskClaimRow.created_at)
                )
            )
            assert statuses == ["superseded", "dispatched"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_root_reclaim_supersedes_stale_dispatched_claim() -> None:
    """F1: root lease expiry re-claim must supersede the old dispatched claim,
    so the reset ready task can actually be re-dispatched instead of dead-locking."""
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=(_node("a", depth=0),), suffix="root-reclaim"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        first = await scheduler.tick(claim)
        assert len(first.dispatched) == 1
        assert first.dispatched[0].status == "dispatched"
        assert await _claim_count(factory, root_job_id=root_job_id) == 1

        # The scheduler crashed; its root lease expires while task "a" is still
        # bound to a child that no worker ever picked up.
        async with factory() as session:
            await session.execute(
                update(JobRow)
                .where(JobRow.id == root_job_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

        async with factory() as session:
            runtime = PostgresRuntime(session)
            reclaimed = await runtime.claim_next_job(
                worker_id="ready-set-scheduler-2", lease_seconds=120
            )
            assert reclaimed is not None and reclaimed.job_id == root_job_id

        async with factory() as session:
            statuses = list(
                await session.scalars(
                    select(PlanTaskClaimRow.status).where(
                        PlanTaskClaimRow.root_job_id == root_job_id
                    )
                )
            )
            assert statuses == ["superseded"]

        second = await scheduler.tick(reclaimed)
        assert len(second.dispatched) == 1
        assert second.dispatched[0].claim_generation == 2
        assert await _claim_count(factory, root_job_id=root_job_id) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_set_retry_closes_terminal_child_allocation() -> None:
    """F2: retry_task must close the terminal child's budget allocation before
    clearing the task-child binding, or committed budget leaks forever."""
    _, engine = _database()
    factory = create_session_factory(engine)
    try:
        root_job_id, basis_hash, claim = await _bootstrap(
            factory, nodes=(_node("a", depth=0),), suffix="retry-budget"
        )
        scheduler = ReadySetScheduler(session_factory=factory, default_concurrency=8)
        first = await scheduler.tick(claim)
        assert len(first.dispatched) == 1

        async with factory() as session:
            committed = int(
                await session.scalar(
                    select(BudgetAccountRow.token_committed).where(
                        BudgetAccountRow.root_job_id == root_job_id
                    )
                )
                or 0
            )
            assert committed == READY_WORKER_PROFILE.token_cap

        assert (
            await _run_child(factory, basis_hash=basis_hash, status=DelegationStatus.FAILED)
            is ResultDisposition.ELIGIBLE
        )
        # No settle tick runs between failure and retry: the old code leaked the
        # committed grant because settle_terminal_children never saw the orphaned child.
        assert await scheduler.retry_task(claim=claim, logical_key="a") is True

        async with factory() as session:
            committed = int(
                await session.scalar(
                    select(BudgetAccountRow.token_committed).where(
                        BudgetAccountRow.root_job_id == root_job_id
                    )
                )
                or 0
            )
            assert committed == 0
    finally:
        await engine.dispose()
