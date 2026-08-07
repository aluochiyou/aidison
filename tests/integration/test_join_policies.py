from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.agents.profiles import RESEARCH_WORKER_PROFILE
from aidison.application.service import ProjectApplication
from aidison.infrastructure.budget import BudgetLedger
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import (
    AttemptRow,
    BudgetAllocationRow,
    BudgetOperationRow,
    DelegationRow,
    JobRow,
)
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import (
    AttemptStatus,
    BudgetOperationKind,
    BudgetOperationState,
    DelegationResult,
    DelegationSpec,
    DelegationStatus,
    DelegationWave,
    JobClaim,
    JobStatus,
    JoinMode,
    JoinPolicy,
    JoinStatus,
    ResultDisposition,
)

pytestmark = pytest.mark.integration


async def _create_wave(
    session: AsyncSession,
    *,
    mode: JoinMode,
    min_successes: int,
    deadline: datetime,
    count: int = 3,
) -> tuple[PostgresRuntime, JobClaim, tuple[DelegationSpec, ...], DelegationWave]:
    await session.execute(text("TRUNCATE TABLE jobs CASCADE"))
    await session.commit()
    project = await ProjectApplication(PostgresDomainStore(session)).create_project(
        name=f"Join policy fixture {uuid4()}",
        goal="Prove durable join closure semantics",
        idempotency_key=f"join-policy-project-{uuid4()}",
    )
    basis_hash = sha256(f"join-policy:{project.id}".encode()).hexdigest()
    runtime = PostgresRuntime(session)
    root_job_id = await runtime.create_job(
        project_id=project.id,
        kind="research_wave",
        basis_hash=basis_hash,
        basis_project_revision=project.revision,
        profile_id="research-orchestrator",
        profile_revision=1,
        token_budget_cap=20_000,
        tool_call_budget_cap=3,
    )
    root_claim = await runtime.claim_next_job(worker_id="join-controller", lease_seconds=300)
    assert root_claim is not None and root_claim.job_id == root_job_id
    specs = tuple(
        DelegationSpec(
            parent_job_id=root_job_id,
            parent_attempt_id=root_claim.attempt_id,
            parent_claim_generation=root_claim.claim_generation,
            graph_step_id=f"join.{mode.value}",
            role_key="research-worker",
            profile_id=RESEARCH_WORKER_PROFILE.profile_id,
            profile_revision=RESEARCH_WORKER_PROFILE.revision,
            basis_hash=basis_hash,
            shard_key=f"shard-{index}",
            idempotency_key=f"{root_claim.attempt_id}:{mode.value}:{index}",
            token_budget=3_000,
            tool_call_budget=1,
            deadline=deadline,
        )
        for index in range(count)
    )
    wave = await runtime.create_delegation_wave(
        specs=specs,
        policy=JoinPolicy(
            mode=mode,
            expected_delegation_ids=tuple(item.delegation_id for item in specs),
            min_successes=min_successes,
            deadline=deadline,
        ),
    )
    return runtime, root_claim, specs, wave


async def _claim_children(
    runtime: PostgresRuntime,
    wave: DelegationWave,
) -> dict[UUID, JobClaim]:
    claims: dict[UUID, JobClaim] = {}
    for index in range(len(wave.child_job_ids)):
        claim = await runtime.claim_next_job(
            worker_id=f"join-worker-{index}",
            lease_seconds=300,
        )
        assert claim is not None and claim.job_id in wave.child_job_ids
        claims[claim.job_id] = claim
    return claims


async def _succeed(
    runtime: PostgresRuntime,
    *,
    spec: DelegationSpec,
    claim: JobClaim,
) -> str:
    registered = await runtime.register_result(
        result=DelegationResult(
            delegation_id=spec.delegation_id,
            child_job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            child_claim_generation=claim.claim_generation,
            status=DelegationStatus.SUCCEEDED,
            basis_hash=spec.basis_hash,
            proposal_ref=f"proposal://{spec.shard_key}",
        ),
        lease_token=claim.lease_token,
    )
    assert registered.disposition is ResultDisposition.ELIGIBLE
    return registered.result_hash


async def _fail(
    runtime: PostgresRuntime,
    *,
    spec: DelegationSpec,
    claim: JobClaim,
) -> None:
    registered = await runtime.register_result(
        result=DelegationResult(
            delegation_id=spec.delegation_id,
            child_job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            child_claim_generation=claim.claim_generation,
            status=DelegationStatus.FAILED,
            basis_hash=spec.basis_hash,
            normalized_error="provider_unavailable",
        ),
        lease_token=claim.lease_token,
    )
    assert registered.disposition is ResultDisposition.ELIGIBLE


@pytest.mark.asyncio
async def test_first_valid_commit_cancels_siblings_reconciles_budget_and_replays() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            runtime, root_claim, specs, wave = await _create_wave(
                session,
                mode=JoinMode.FIRST_VALID,
                min_successes=1,
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            )
            claims = await _claim_children(runtime, wave)
            spec_by_child = dict(zip(wave.child_job_ids, specs, strict=True))
            winner_id, dispatched_id, reserved_id = wave.child_job_ids

            ledger = BudgetLedger(session)
            allocation_ids = {
                child_id: (
                    await session.scalar(
                        select(BudgetAllocationRow.id).where(
                            BudgetAllocationRow.owner_ref == child_id
                        )
                    )
                )
                for child_id in wave.child_job_ids
            }
            assert all(allocation_ids.values())
            dispatched = await ledger.reserve_operation(
                allocation_id=allocation_ids[dispatched_id],  # type: ignore[arg-type]
                claim=claims[dispatched_id],
                kind=BudgetOperationKind.MODEL,
                logical_step="join.first_valid.dispatched",
                physical_attempt_no=1,
                idempotency_key=f"join-operation-{uuid4()}",
                request_hash="a" * 64,
                provider="fake",
                model_or_tool="fake-model",
                reserved_tokens=500,
            )
            await ledger.mark_dispatched(dispatched.operation_id)
            reserved = await ledger.reserve_operation(
                allocation_id=allocation_ids[reserved_id],  # type: ignore[arg-type]
                claim=claims[reserved_id],
                kind=BudgetOperationKind.MODEL,
                logical_step="join.first_valid.reserved",
                physical_attempt_no=1,
                idempotency_key=f"join-operation-{uuid4()}",
                request_hash="b" * 64,
                provider="fake",
                model_or_tool="fake-model",
                reserved_tokens=400,
            )
            await session.commit()

            winner_hash = await _succeed(
                runtime,
                spec=spec_by_child[winner_id],
                claim=claims[winner_id],
            )
            snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert snapshot.ready is True
            assert snapshot.impossible is False
            assert snapshot.accepted_proposal_refs == ("proposal://shard-0",)

            receipt = await runtime.commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref="proposal://first-valid-merged",
            )
            replay = await runtime.commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref="proposal://ignored",
            )
            assert receipt is not None and replay == receipt
            assert receipt.accepted_result_hashes == (winner_hash,)

            jobs = {
                row.id: row
                for row in await session.scalars(
                    select(JobRow).where(JobRow.id.in_(wave.child_job_ids))
                )
            }
            assert jobs[winner_id].status == JobStatus.SUCCEEDED.value
            assert jobs[dispatched_id].status == JobStatus.CANCELLED.value
            assert jobs[reserved_id].status == JobStatus.CANCELLED.value
            assert jobs[dispatched_id].cancel_requested is True
            sibling_attempts = list(
                await session.scalars(
                    select(AttemptRow).where(AttemptRow.job_id.in_((dispatched_id, reserved_id)))
                )
            )
            assert {item.status for item in sibling_attempts} == {AttemptStatus.CANCELLED.value}
            allocations = list(
                await session.scalars(
                    select(BudgetAllocationRow).where(
                        BudgetAllocationRow.owner_ref.in_((dispatched_id, reserved_id))
                    )
                )
            )
            assert {item.status for item in allocations} == {"closed"}
            dispatched_row = await session.get(BudgetOperationRow, dispatched.operation_id)
            reserved_row = await session.get(BudgetOperationRow, reserved.operation_id)
            assert dispatched_row is not None
            assert dispatched_row.state == BudgetOperationState.AMBIGUOUS.value
            assert dispatched_row.consumed_tokens == 500
            assert dispatched_row.normalized_error == "first_valid_sibling_cancelled"
            assert reserved_row is not None
            assert reserved_row.state == BudgetOperationState.RELEASED.value

            late = await runtime.register_result(
                result=DelegationResult(
                    delegation_id=spec_by_child[dispatched_id].delegation_id,
                    child_job_id=dispatched_id,
                    attempt_id=claims[dispatched_id].attempt_id,
                    child_claim_generation=claims[dispatched_id].claim_generation,
                    status=DelegationStatus.SUCCEEDED,
                    basis_hash=spec_by_child[dispatched_id].basis_hash,
                    proposal_ref="proposal://late",
                ),
                lease_token=claims[dispatched_id].lease_token,
            )
            assert late.disposition is ResultDisposition.QUARANTINED
            assert "join_closed" in (late.quarantine_reason or "")
            assert await runtime.claim_next_job(worker_id="should-find-no-sibling") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_valid_winner_is_deterministic_for_concurrent_successes() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            runtime, root_claim, specs, wave = await _create_wave(
                session,
                mode=JoinMode.FIRST_VALID,
                min_successes=1,
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            )
            claims = await _claim_children(runtime, wave)
            spec_by_child = dict(zip(wave.child_job_ids, specs, strict=True))
            result_hashes: dict[UUID, str] = {}
            for child_id in wave.child_job_ids[:2]:
                result_hashes[spec_by_child[child_id].delegation_id] = await _succeed(
                    runtime,
                    spec=spec_by_child[child_id],
                    claim=claims[child_id],
                )
            tied_at = datetime.now(UTC)
            tied_ids = tuple(item.delegation_id for item in specs[:2])
            await session.execute(
                update(DelegationRow)
                .where(DelegationRow.id.in_(tied_ids))
                .values(completed_at=tied_at)
            )
            await session.commit()
            winner_delegation_id = min(tied_ids)
            winner_spec = next(item for item in specs if item.delegation_id == winner_delegation_id)

            snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert snapshot.accepted_proposal_refs == (f"proposal://{winner_spec.shard_key}",)
            assert any(
                item.reason == "first_valid_superseded" for item in snapshot.rejected_results
            )
            receipt = await runtime.commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref="proposal://deterministic-winner",
            )
            assert receipt is not None
            assert receipt.accepted_result_hashes == (result_hashes[winner_delegation_id],)
            assert any(item.reason == "first_valid_superseded" for item in receipt.rejected_results)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_valid_all_terminal_without_success_fails_and_leaves_no_work() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            runtime, root_claim, specs, wave = await _create_wave(
                session,
                mode=JoinMode.FIRST_VALID,
                min_successes=1,
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            )
            claims = await _claim_children(runtime, wave)
            for child_id, spec in zip(wave.child_job_ids, specs, strict=True):
                await _fail(runtime, spec=spec, claim=claims[child_id])

            snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert snapshot.status is JoinStatus.FAILED
            assert snapshot.ready is False
            assert snapshot.impossible is True
            assert (
                await runtime.claim_next_job(worker_id="no-work-after-first-valid-failure")
                is None
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bounded_partial_waits_for_boundary_and_accepts_all_successes() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            runtime, root_claim, specs, wave = await _create_wave(
                session,
                mode=JoinMode.BOUNDED_PARTIAL,
                min_successes=2,
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            )
            claims = await _claim_children(runtime, wave)
            spec_by_child = dict(zip(wave.child_job_ids, specs, strict=True))
            hashes = []
            for child_id in wave.child_job_ids[:2]:
                hashes.append(
                    await _succeed(
                        runtime,
                        spec=spec_by_child[child_id],
                        claim=claims[child_id],
                    )
                )
            before_boundary = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert before_boundary.ready is False
            assert before_boundary.impossible is False
            assert (
                await runtime.commit_join(
                    join_group_id=wave.join_group_id,
                    merged_proposal_ref="proposal://too-early",
                )
                is None
            )

            last_id = wave.child_job_ids[-1]
            hashes.append(
                await _succeed(
                    runtime,
                    spec=spec_by_child[last_id],
                    claim=claims[last_id],
                )
            )
            at_boundary = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert at_boundary.ready is True
            assert len(at_boundary.accepted_proposal_refs) == 3
            receipt = await runtime.commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref="proposal://bounded-merged",
            )
            assert receipt is not None
            assert set(receipt.accepted_result_hashes) == set(hashes)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bounded_partial_deadline_commits_threshold_and_cancels_running_siblings() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            runtime, root_claim, specs, wave = await _create_wave(
                session,
                mode=JoinMode.BOUNDED_PARTIAL,
                min_successes=1,
                deadline=datetime.now(UTC) - timedelta(seconds=1),
            )
            claims = await _claim_children(runtime, wave)
            spec_by_child = dict(zip(wave.child_job_ids, specs, strict=True))
            winner_id = wave.child_job_ids[0]
            winner_hash = await _succeed(
                runtime,
                spec=spec_by_child[winner_id],
                claim=claims[winner_id],
            )
            snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert snapshot.ready is True
            assert snapshot.impossible is False
            assert snapshot.accepted_proposal_refs == ("proposal://shard-0",)

            receipt = await runtime.commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref="proposal://bounded-at-deadline",
            )
            assert receipt is not None
            assert receipt.accepted_result_hashes == (winner_hash,)
            siblings = list(
                await session.scalars(select(JobRow).where(JobRow.id.in_(wave.child_job_ids[1:])))
            )
            assert {item.status for item in siblings} == {JobStatus.CANCELLED.value}
            assert {item.reason for item in receipt.rejected_results} == {
                DelegationStatus.CANCELLED.value
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "min_successes"),
    (
        (JoinMode.BOUNDED_PARTIAL, 2),
        (JoinMode.FIRST_VALID, 1),
    ),
)
async def test_expired_join_cancels_all_children_and_leaves_no_runnable_work(
    mode: JoinMode,
    min_successes: int,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            runtime, root_claim, _, wave = await _create_wave(
                session,
                mode=mode,
                min_successes=min_successes,
                deadline=datetime.now(UTC) - timedelta(seconds=1),
            )
            await _claim_children(runtime, wave)
            snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert snapshot.status is JoinStatus.EXPIRED
            assert snapshot.ready is False
            assert snapshot.impossible is True
            jobs = list(
                await session.scalars(select(JobRow).where(JobRow.id.in_(wave.child_job_ids)))
            )
            assert {item.status for item in jobs} == {JobStatus.CANCELLED.value}
            allocations = list(
                await session.scalars(
                    select(BudgetAllocationRow).where(
                        BudgetAllocationRow.owner_ref.in_(wave.child_job_ids)
                    )
                )
            )
            assert {item.status for item in allocations} == {"closed"}
            assert await runtime.claim_next_job(worker_id="should-find-no-expired-child") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_commit_join_closes_impossible_group_without_prior_inspection() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            runtime, root_claim, _, wave = await _create_wave(
                session,
                mode=JoinMode.BOUNDED_PARTIAL,
                min_successes=2,
                deadline=datetime.now(UTC) - timedelta(seconds=1),
            )
            await _claim_children(runtime, wave)
            assert (
                await runtime.commit_join(
                    join_group_id=wave.join_group_id,
                    merged_proposal_ref="proposal://impossible",
                )
                is None
            )
            snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert snapshot.status is JoinStatus.EXPIRED
            assert snapshot.impossible is True
            assert await runtime.claim_next_job(worker_id="no-work-after-direct-commit") is None
    finally:
        await engine.dispose()
