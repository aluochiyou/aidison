from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update

from aidison.agents.profiles import RESEARCH_WORKER_PROFILE
from aidison.application.service import ProjectApplication
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import (
    AttemptResultRow,
    AttemptRow,
    BudgetAllocationRow,
    DelegationRow,
    JobRow,
    JoinGroupRow,
    JoinReceiptRow,
    ProjectRow,
)
from aidison.infrastructure.runtime import PostgresRuntime, RuntimeConflictError
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import (
    DelegationResult,
    DelegationSpec,
    DelegationStatus,
    JobStatus,
    JoinMode,
    JoinPolicy,
    ResultDisposition,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_eight_child_join_is_deterministic_when_results_arrive_out_of_order() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE jobs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Eight-way runtime fixture",
                goal="Prove bounded N-way fan-out and deterministic fan-in",
                idempotency_key=f"nway-project-{uuid4()}",
            )
            basis_hash = sha256(b"nway-runtime-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
                token_budget_cap=8_000,
                tool_call_budget_cap=0,
            )
            root_claim = await runtime.claim_next_job(
                worker_id="nway-controller",
                lease_seconds=60,
            )
            assert root_claim is not None

            deadline = datetime.now(UTC) + timedelta(minutes=5)
            specs = tuple(
                DelegationSpec(
                    parent_job_id=root_job_id,
                    parent_attempt_id=root_claim.attempt_id,
                    parent_claim_generation=root_claim.claim_generation,
                    graph_step_id="research.nway",
                    profile_id=RESEARCH_WORKER_PROFILE.profile_id,
                    profile_revision=RESEARCH_WORKER_PROFILE.revision,
                    basis_hash=basis_hash,
                    shard_key=f"shard-{index + 1}",
                    idempotency_key=f"{root_claim.attempt_id}:research.nway:{index + 1}",
                    token_budget=1_000,
                    tool_call_budget=0,
                    deadline=deadline,
                )
                for index in range(8)
            )
            wave = await runtime.create_delegation_wave(
                specs=specs,
                policy=JoinPolicy(
                    mode=JoinMode.ALL_REQUIRED,
                    expected_delegation_ids=tuple(item.delegation_id for item in specs),
                    min_successes=8,
                    deadline=deadline,
                ),
            )
            assert len(wave.child_job_ids) == 8

            claims = []
            for index in range(8):
                claim = await runtime.claim_next_job(
                    worker_id=f"nway-worker-{index + 1}",
                    lease_seconds=60,
                )
                assert claim is not None
                claims.append(claim)
            spec_by_child = dict(zip(wave.child_job_ids, specs, strict=True))
            registered_hashes = []
            for claim in reversed(claims):
                spec = spec_by_child[claim.job_id]
                registered = await runtime.register_result(
                    result=DelegationResult(
                        delegation_id=spec.delegation_id,
                        child_job_id=claim.job_id,
                        attempt_id=claim.attempt_id,
                        child_claim_generation=claim.claim_generation,
                        status=DelegationStatus.SUCCEEDED,
                        basis_hash=basis_hash,
                        proposal_ref=f"proposal://{spec.shard_key}",
                    ),
                    lease_token=claim.lease_token,
                )
                assert registered.disposition is ResultDisposition.ELIGIBLE
                registered_hashes.append(registered.result_hash)

            snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert snapshot.ready is True
            assert snapshot.impossible is False
            assert set(snapshot.accepted_proposal_refs) == {
                f"proposal://shard-{index + 1}" for index in range(8)
            }
            receipt = await runtime.commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref="proposal://nway-merged",
            )
            assert receipt is not None
            assert set(receipt.accepted_result_hashes) == set(registered_hashes)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generation_fencing_quarantines_late_result_and_join_is_unique() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE jobs CASCADE"))
            await session.commit()
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="Runtime fixture",
                goal="Prove durable two-child fan-out and fan-in",
                idempotency_key=f"runtime-project-{uuid4()}",
            )
            basis_hash = sha256(b"runtime-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            root_claim = await runtime.claim_next_job(worker_id="controller", lease_seconds=60)
            assert root_claim is not None
            assert root_claim.job_id == root_job_id

            specs = tuple(
                DelegationSpec(
                    parent_job_id=root_job_id,
                    parent_attempt_id=root_claim.attempt_id,
                    parent_claim_generation=root_claim.claim_generation,
                    graph_step_id="research.parallel",
                    profile_id="research-worker-ro",
                    profile_revision=RESEARCH_WORKER_PROFILE.revision,
                    basis_hash=basis_hash,
                    shard_key=f"module-{index}",
                    idempotency_key=f"{root_job_id}:research.parallel:{index}",
                    token_budget=1_000,
                    tool_call_budget=2,
                    deadline=datetime.now(UTC) + timedelta(minutes=5),
                )
                for index in range(2)
            )
            policy = JoinPolicy(
                mode=JoinMode.ALL_REQUIRED,
                expected_delegation_ids=tuple(item.delegation_id for item in specs),
                min_successes=2,
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            )
            wave = await runtime.create_delegation_wave(specs=specs, policy=policy)
            allocation_ids = tuple(
                await session.scalars(
                    select(BudgetAllocationRow.id)
                    .where(BudgetAllocationRow.owner_kind == "child")
                    .order_by(BudgetAllocationRow.id)
                )
            )
            replayed_wave = await runtime.create_delegation_wave(specs=specs, policy=policy)
            assert replayed_wave == wave
            assert len(allocation_ids) == 2
            assert tuple(
                await session.scalars(
                    select(BudgetAllocationRow.id)
                    .where(BudgetAllocationRow.owner_kind == "child")
                    .order_by(BudgetAllocationRow.id)
                )
            ) == allocation_ids
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(DelegationRow)
                    .where(DelegationRow.join_group_id == wave.join_group_id)
                )
                == 2
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(JobRow)
                    .where(JobRow.parent_job_id == root_job_id)
                )
                == 2
            )

            first_claim = await runtime.claim_next_job(worker_id="worker-a", lease_seconds=60)
            second_claim = await runtime.claim_next_job(worker_id="worker-b", lease_seconds=60)
            assert first_claim is not None
            assert second_claim is not None
            assert {first_claim.job_id, second_claim.job_id} == set(wave.child_job_ids)
            second_allocation_id = await session.scalar(
                select(BudgetAllocationRow.id).where(
                    BudgetAllocationRow.owner_ref == second_claim.job_id
                )
            )
            assert second_allocation_id is not None

            spec_by_child = dict(zip(wave.child_job_ids, specs, strict=True))
            first_spec = spec_by_child[first_claim.job_id]
            first_result = DelegationResult(
                delegation_id=first_spec.delegation_id,
                child_job_id=first_claim.job_id,
                attempt_id=first_claim.attempt_id,
                child_claim_generation=first_claim.claim_generation,
                status=DelegationStatus.SUCCEEDED,
                basis_hash=basis_hash,
                proposal_ref="proposal://worker-a",
            )
            registered_first = await runtime.register_result(
                result=first_result,
                lease_token=first_claim.lease_token,
            )
            assert registered_first.disposition is ResultDisposition.ELIGIBLE

            await session.execute(
                update(JobRow)
                .where(JobRow.id == second_claim.job_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
            reclaimed = await runtime.claim_next_job(worker_id="worker-c", lease_seconds=60)
            assert reclaimed is not None
            assert reclaimed.job_id == second_claim.job_id
            assert reclaimed.claim_generation == second_claim.claim_generation + 1
            assert (
                await session.scalar(
                    select(BudgetAllocationRow.id).where(
                        BudgetAllocationRow.owner_ref == reclaimed.job_id
                    )
                )
                == second_allocation_id
            )

            second_spec = spec_by_child[second_claim.job_id]
            late_result = DelegationResult(
                delegation_id=second_spec.delegation_id,
                child_job_id=second_claim.job_id,
                attempt_id=second_claim.attempt_id,
                child_claim_generation=second_claim.claim_generation,
                status=DelegationStatus.SUCCEEDED,
                basis_hash=basis_hash,
                proposal_ref="proposal://late-worker-b",
            )
            registered_late = await runtime.register_result(
                result=late_result,
                lease_token=second_claim.lease_token,
            )
            assert registered_late.disposition is ResultDisposition.QUARANTINED
            assert "child_generation_or_lease_stale" in (registered_late.quarantine_reason or "")

            current_result = DelegationResult(
                delegation_id=second_spec.delegation_id,
                child_job_id=reclaimed.job_id,
                attempt_id=reclaimed.attempt_id,
                child_claim_generation=reclaimed.claim_generation,
                status=DelegationStatus.SUCCEEDED,
                basis_hash=basis_hash,
                proposal_ref="proposal://worker-c",
            )
            registered_current = await runtime.register_result(
                result=current_result,
                lease_token=reclaimed.lease_token,
            )
            assert registered_current.disposition is ResultDisposition.ELIGIBLE

            join_snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_claim,
            )
            assert join_snapshot.ready is True
            assert join_snapshot.impossible is False
            assert set(join_snapshot.accepted_proposal_refs) == {
                "proposal://worker-a",
                "proposal://worker-c",
            }

            receipt = await runtime.commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref="proposal://merged",
            )
            duplicate_receipt = await runtime.commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref="proposal://ignored-on-replay",
            )
            assert receipt is not None
            assert duplicate_receipt == receipt
            assert set(receipt.accepted_result_hashes) == {
                registered_first.result_hash,
                registered_current.result_hash,
            }
            for inconsistent_values in (
                {"basis_hash": sha256(b"wrong-receipt-basis").hexdigest()},
                {"parent_claim_generation": root_claim.claim_generation + 1},
            ):
                await session.execute(
                    update(JoinReceiptRow)
                    .where(JoinReceiptRow.join_group_id == wave.join_group_id)
                    .values(**inconsistent_values)
                )
                await session.commit()
                with pytest.raises(RuntimeConflictError, match="does not match"):
                    await runtime.find_committed_join(
                        parent_claim=root_claim,
                        graph_step_id="research.parallel",
                    )
                await session.execute(
                    update(JoinReceiptRow)
                    .where(JoinReceiptRow.join_group_id == wave.join_group_id)
                    .values(
                        basis_hash=receipt.basis_hash,
                        parent_claim_generation=receipt.parent_claim_generation,
                    )
                )
                await session.commit()
            committed = await runtime.find_committed_join(
                parent_claim=root_claim,
                graph_step_id="research.parallel",
            )
            assert committed is not None and committed.receipt == receipt
            assert await runtime.complete_claim(
                claim=root_claim,
                status=JobStatus.SUCCEEDED,
                result_ref=receipt.merged_proposal_ref,
            )
            with pytest.raises(RuntimeConflictError, match="stale or terminal"):
                await runtime.complete_claim(
                    claim=root_claim,
                    status=JobStatus.SUCCEEDED,
                    result_ref=receipt.merged_proposal_ref,
                )

            dispositions = list(
                await session.scalars(
                    select(AttemptResultRow.disposition)
                    .where(AttemptResultRow.project_id == project.id)
                    .order_by(AttemptResultRow.created_at)
                )
            )
            assert dispositions.count(ResultDisposition.ELIGIBLE.value) == 2
            assert dispositions.count(ResultDisposition.QUARANTINED.value) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(JoinReceiptRow)
                    .where(JoinReceiptRow.join_group_id == wave.join_group_id)
                )
                == 1
            )
            stored_project = await session.get(ProjectRow, project.id)
            assert stored_project is not None
            assert stored_project.revision == 1
            assert stored_project.event_sequence >= 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parent_cancel_propagates_and_late_child_result_is_quarantined() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE jobs CASCADE"))
            await session.commit()
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="Cancellation fixture",
                goal="Prove parent cancellation propagation",
                idempotency_key=f"cancel-project-{uuid4()}",
            )
            basis_hash = sha256(b"cancel-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            root_claim = await runtime.claim_next_job(worker_id="controller")
            assert root_claim is not None
            spec = DelegationSpec(
                parent_job_id=root_job_id,
                parent_attempt_id=root_claim.attempt_id,
                parent_claim_generation=root_claim.claim_generation,
                graph_step_id="research.cancel",
                profile_id="research-worker-ro",
                profile_revision=RESEARCH_WORKER_PROFILE.revision,
                basis_hash=basis_hash,
                shard_key="module",
                idempotency_key=f"{root_job_id}:cancel:module",
                token_budget=1_000,
                tool_call_budget=2,
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            )
            wave = await runtime.create_delegation_wave(
                specs=(spec,),
                policy=JoinPolicy(
                    mode=JoinMode.ALL_REQUIRED,
                    expected_delegation_ids=(spec.delegation_id,),
                    min_successes=1,
                    deadline=datetime.now(UTC) + timedelta(minutes=5),
                ),
            )
            child_claim = await runtime.claim_next_job(worker_id="worker")
            assert child_claim is not None
            assert child_claim.job_id == wave.child_job_ids[0]

            assert await runtime.cancel_job(root_job_id) is True
            assert await runtime.cancel_job(root_job_id) is False
            late = await runtime.register_result(
                result=DelegationResult(
                    delegation_id=spec.delegation_id,
                    child_job_id=child_claim.job_id,
                    attempt_id=child_claim.attempt_id,
                    child_claim_generation=child_claim.claim_generation,
                    status=DelegationStatus.SUCCEEDED,
                    basis_hash=basis_hash,
                    proposal_ref="proposal://after-cancel",
                ),
                lease_token=child_claim.lease_token,
            )
            assert late.disposition is ResultDisposition.QUARANTINED
            assert "join_closed" in (late.quarantine_reason or "")
            assert (
                await runtime.commit_join(
                    join_group_id=wave.join_group_id,
                    merged_proposal_ref="proposal://must-not-exist",
                )
                is None
            )

            root = await session.get(JobRow, root_job_id)
            child = await session.get(JobRow, child_claim.job_id)
            group = await session.get(JoinGroupRow, wave.join_group_id)
            assert root is not None and root.status == "cancelled"
            assert child is not None and child.status == "cancelled"
            assert group is not None and group.status == "cancelled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_child_closes_all_required_join_as_impossible() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE jobs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Failed join fixture",
                goal="Prove an impossible ALL_REQUIRED join terminates",
                idempotency_key=f"failed-join-project-{uuid4()}",
            )
            basis_hash = sha256(b"failed-join-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            root_work = await runtime.claim_next_work(worker_id="controller")
            assert root_work is not None
            assert root_work.project_id == project.id
            assert root_work.kind == "research_wave"
            assert root_work.delegation is None

            spec = DelegationSpec(
                parent_job_id=root_job_id,
                parent_attempt_id=root_work.claim.attempt_id,
                parent_claim_generation=root_work.claim.claim_generation,
                graph_step_id="research.failure",
                profile_id="research-worker-ro",
                profile_revision=RESEARCH_WORKER_PROFILE.revision,
                basis_hash=basis_hash,
                shard_key="only-child",
                idempotency_key=f"{root_work.claim.attempt_id}:failure:only-child",
                token_budget=1_000,
                tool_call_budget=1,
                deadline=datetime.now(UTC) + timedelta(minutes=5),
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
            child_work = await runtime.claim_next_work(worker_id="worker")
            assert child_work is not None
            assert child_work.delegation == spec
            failed = await runtime.register_result(
                result=DelegationResult(
                    delegation_id=spec.delegation_id,
                    child_job_id=child_work.claim.job_id,
                    attempt_id=child_work.claim.attempt_id,
                    child_claim_generation=child_work.claim.claim_generation,
                    status=DelegationStatus.FAILED,
                    basis_hash=basis_hash,
                    normalized_error="provider_unavailable",
                ),
                lease_token=child_work.claim.lease_token,
            )
            assert failed.disposition is ResultDisposition.ELIGIBLE
            failed_attempt = await session.get(AttemptRow, child_work.claim.attempt_id)
            assert failed_attempt is not None
            assert failed_attempt.normalized_error == "provider_unavailable"

            snapshot = await runtime.inspect_join(
                join_group_id=wave.join_group_id,
                parent_claim=root_work.claim,
            )
            assert snapshot.ready is False
            assert snapshot.impossible is True
            assert snapshot.status.value == "failed"
            assert (
                await runtime.commit_join(
                    join_group_id=wave.join_group_id,
                    merged_proposal_ref="artifact+sha256://must-not-exist",
                )
                is None
            )
            assert await runtime.complete_claim(
                claim=root_work.claim,
                status=JobStatus.FAILED,
                normalized_error="join_impossible",
            )
    finally:
        await engine.dispose()
