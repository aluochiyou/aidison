from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aidison.agents.profiles import RESEARCH_WORKER_PROFILE
from aidison.application.execution import DurableJoinWaiter
from aidison.application.service import ProjectApplication
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import JoinGroupRow
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.signals import PostgresSignalBus, signal_listener_name
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import (
    DelegationResult,
    DelegationSpec,
    DelegationStatus,
    DelegationWave,
    JobClaim,
    JoinMode,
    JoinPolicy,
)

pytestmark = pytest.mark.integration


async def _create_single_child_join(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[Any, JobClaim, DelegationWave, JobClaim, DelegationSpec]:
    async with factory() as session:
        await session.execute(text("TRUNCATE TABLE jobs CASCADE"))
        await session.commit()
        project = await ProjectApplication(PostgresDomainStore(session)).create_project(
            name="Join signal fixture",
            goal="Prove durable state plus low-latency wake",
            idempotency_key=f"join-signal-project-{uuid4()}",
        )
        basis_hash = sha256(f"join-signal-{uuid4()}".encode()).hexdigest()
        runtime = PostgresRuntime(session)
        root_job_id = await runtime.create_job(
            project_id=project.id,
            kind="research_wave",
            basis_hash=basis_hash,
            basis_project_revision=project.revision,
            profile_id="research-orchestrator",
            profile_revision=1,
            token_budget_cap=4_000,
            tool_call_budget_cap=0,
        )
        parent_claim = await runtime.claim_next_job(worker_id="signal-parent", lease_seconds=60)
        assert parent_claim is not None
        spec = DelegationSpec(
            parent_job_id=root_job_id,
            parent_attempt_id=parent_claim.attempt_id,
            parent_claim_generation=parent_claim.claim_generation,
            graph_step_id="signal.join",
            profile_id=RESEARCH_WORKER_PROFILE.profile_id,
            profile_revision=RESEARCH_WORKER_PROFILE.revision,
            basis_hash=basis_hash,
            shard_key="signal-shard",
            idempotency_key=f"{parent_claim.attempt_id}:signal.join",
            token_budget=1_000,
            tool_call_budget=0,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
        wave = await runtime.create_delegation_wave(
            specs=(spec,),
            policy=JoinPolicy(
                mode=JoinMode.ALL_REQUIRED,
                expected_delegation_ids=(spec.delegation_id,),
                min_successes=1,
                deadline=spec.deadline,
            ),
        )
        child_claim = await runtime.claim_next_job(worker_id="signal-child", lease_seconds=60)
        assert child_claim is not None
        assert child_claim.job_id == wave.child_job_ids[0]
        return project, parent_claim, wave, child_claim, spec


async def _register_success(
    factory: async_sessionmaker[AsyncSession],
    *,
    claim: JobClaim,
    spec: DelegationSpec,
) -> None:
    async with factory() as session:
        await PostgresRuntime(session).register_result(
            result=DelegationResult(
                delegation_id=spec.delegation_id,
                child_job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                child_claim_generation=claim.claim_generation,
                status=DelegationStatus.SUCCEEDED,
                basis_hash=claim.basis_hash,
                proposal_ref="artifact://signal-result",
            ),
            lease_token=claim.lease_token,
        )


@pytest.mark.asyncio
async def test_committed_domain_event_wakes_matching_project_subscription() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Signal fixture",
                goal="Wake a durable join waiter after commit",
                idempotency_key=f"signal-project-{uuid4()}",
            )

        bus = PostgresSignalBus(engine)
        async with bus.subscribe(project_id=project.id) as subscription:
            async with factory() as session:
                store = PostgresDomainStore(session)
                sequence = await store.append_event(
                    project.id,
                    "delegation.result_registered",
                    {"join_group_id": str(uuid4())},
                )
                await session.commit()

            signal = await subscription.wait(timeout_seconds=1)

        assert signal is not None
        assert signal.project_id == project.id
        assert signal.project_sequence == sequence
        assert signal.event_type == "delegation.result_registered"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rolled_back_domain_event_does_not_wake_subscription() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Rollback signal fixture",
                goal="Never wake on an uncommitted state transition",
                idempotency_key=f"rollback-signal-project-{uuid4()}",
            )

        bus = PostgresSignalBus(engine)
        async with bus.subscribe(project_id=project.id) as subscription:
            async with factory() as session:
                await PostgresDomainStore(session).append_event(
                    project.id,
                    "delegation.result_registered",
                    {"join_group_id": str(uuid4())},
                )
                await session.rollback()

            signal = await subscription.wait(timeout_seconds=0.1)

        assert signal is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unrelated_project_signal_is_filtered_without_losing_matching_signal() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            app = ProjectApplication(PostgresDomainStore(session))
            matching = await app.create_project(
                name="Matching signal fixture",
                goal="Receive only its own wake hint",
                idempotency_key=f"matching-signal-project-{uuid4()}",
            )
            unrelated = await app.create_project(
                name="Unrelated signal fixture",
                goal="Must not wake another project",
                idempotency_key=f"unrelated-signal-project-{uuid4()}",
            )

        bus = PostgresSignalBus(engine)
        async with bus.subscribe(project_id=matching.id) as subscription:
            async with factory() as session:
                store = PostgresDomainStore(session)
                await store.append_event(unrelated.id, "job.claimed", {"job_id": str(uuid4())})
                expected_sequence = await store.append_event(
                    matching.id,
                    "delegation.result_registered",
                    {"join_group_id": str(uuid4())},
                )
                await session.commit()

            signal = await asyncio.wait_for(subscription.wait(timeout_seconds=1), timeout=2)

        assert signal is not None
        assert signal.project_id == matching.id
        assert signal.project_sequence == expected_sequence
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_listener_is_removed_before_connection_returns_to_pool() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_async_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Listener cleanup fixture",
                goal="Never leak a callback into a later pooled checkout",
                idempotency_key=f"listener-cleanup-project-{uuid4()}",
            )

        bus = PostgresSignalBus(engine)
        async with bus.subscribe(project_id=project.id) as subscription:
            assert await subscription.wait(timeout_seconds=0.01) is None

        async with factory() as session:
            await PostgresDomainStore(session).append_event(
                project.id,
                "job.claimed",
                {"job_id": str(uuid4())},
            )
            await session.commit()

        assert await subscription.wait(timeout_seconds=0.1) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_join_waiter_rechecks_durable_state_when_notification_was_before_subscribe() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        project, parent_claim, wave, child_claim, spec = await _create_single_child_join(factory)
        await _register_success(factory, claim=child_claim, spec=spec)

        waiter = DurableJoinWaiter(
            session_factory=factory,
            signal_bus=PostgresSignalBus(engine),
            durable_recheck_seconds=10,
        )
        snapshot = await asyncio.wait_for(
            waiter.wait(
                project_id=project.id,
                join_group_id=wave.join_group_id,
                parent_claim=parent_claim,
            ),
            timeout=1,
        )

        assert snapshot.ready is True
        assert snapshot.accepted_proposal_refs == ("artifact://signal-result",)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_join_waiter_wakes_before_long_durable_recheck_timeout() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        project, parent_claim, wave, child_claim, spec = await _create_single_child_join(factory)
        waiter = DurableJoinWaiter(
            session_factory=factory,
            signal_bus=PostgresSignalBus(engine),
            durable_recheck_seconds=10,
        )
        waiting = asyncio.create_task(
            waiter.wait(
                project_id=project.id,
                join_group_id=wave.join_group_id,
                parent_claim=parent_claim,
            )
        )
        await asyncio.sleep(0.05)
        await _register_success(factory, claim=child_claim, spec=spec)

        snapshot = await asyncio.wait_for(waiting, timeout=1)

        assert snapshot.ready is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_join_waiter_falls_back_to_durable_recheck_when_listen_is_unavailable() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    unavailable_signal_engine = create_async_engine(
        "postgresql+asyncpg://aidison@127.0.0.1:1/aidison_test",
        connect_args={"timeout": 0.05},
    )
    factory = create_session_factory(engine)
    try:
        project, parent_claim, wave, child_claim, spec = await _create_single_child_join(factory)
        waiter = DurableJoinWaiter(
            session_factory=factory,
            signal_bus=PostgresSignalBus(unavailable_signal_engine),
            durable_recheck_seconds=0.05,
        )
        waiting = asyncio.create_task(
            waiter.wait(
                project_id=project.id,
                join_group_id=wave.join_group_id,
                parent_claim=parent_claim,
            )
        )
        await asyncio.sleep(0.1)
        await _register_success(factory, claim=child_claim, spec=spec)

        snapshot = await asyncio.wait_for(waiting, timeout=1)

        assert snapshot.ready is True
    finally:
        await unavailable_signal_engine.dispose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_open_join_inspection_does_not_wait_for_writer_row_lock() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        _, parent_claim, wave, _, _ = await _create_single_child_join(factory)
        async with factory() as locker:
            locked = await locker.scalar(
                select(JoinGroupRow)
                .where(JoinGroupRow.id == wave.join_group_id)
                .with_for_update()
            )
            assert locked is not None

            async with factory() as reader:
                snapshot = await asyncio.wait_for(
                    PostgresRuntime(reader).inspect_join(
                        join_group_id=wave.join_group_id,
                        parent_claim=parent_claim,
                    ),
                    timeout=0.2,
                )

            assert snapshot.ready is False
            assert snapshot.impossible is False
            await locker.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_join_waiter_recovers_when_listener_connection_dies_mid_join() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        project, parent_claim, wave, child_claim, spec = await _create_single_child_join(factory)
        waiter = DurableJoinWaiter(
            session_factory=factory,
            signal_bus=PostgresSignalBus(engine),
            durable_recheck_seconds=10,
            signal_failure_recheck_seconds=0.05,
        )
        waiting = asyncio.create_task(
            waiter.wait(
                project_id=project.id,
                join_group_id=wave.join_group_id,
                parent_claim=parent_claim,
            )
        )

        listener_pid = None
        for _ in range(50):
            async with factory() as session:
                listener_pid = await session.scalar(
                    text(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE application_name = :application_name "
                        "ORDER BY backend_start DESC LIMIT 1"
                    ),
                    {"application_name": signal_listener_name(project.id)},
                )
                await session.commit()
            if listener_pid is not None:
                break
            await asyncio.sleep(0.01)
        assert listener_pid is not None

        async with factory() as session:
            terminated = await session.scalar(
                text("SELECT pg_terminate_backend(:pid)"),
                {"pid": listener_pid},
            )
            await session.commit()
        assert terminated is True

        await _register_success(factory, claim=child_claim, spec=spec)
        snapshot = await asyncio.wait_for(waiting, timeout=1)

        assert snapshot.ready is True
        assert snapshot.accepted_proposal_refs == ("artifact://signal-result",)
    finally:
        await engine.dispose()
