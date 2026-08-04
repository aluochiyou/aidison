from __future__ import annotations

import asyncio
import os
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.agents.profiles import RESEARCH_WORKER_PROFILE, build_profile_revision
from aidison.application.service import ProjectApplication
from aidison.infrastructure.budget import (
    BudgetClaimStaleError,
    BudgetLedger,
    BudgetLimitExceededError,
)
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import BudgetOperationRow
from aidison.infrastructure.profiles import ProfileConflictError, ProfileRepository
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import (
    BudgetOperationKind,
    BudgetOperationState,
    BudgetOwnerKind,
    DelegationSpec,
    JobClaim,
    JoinMode,
    JoinPolicy,
)


def _factory() -> tuple[async_sessionmaker[AsyncSession], object]:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    return create_session_factory(engine), engine


async def _create_child_allocation(
    factory: async_sessionmaker[AsyncSession],
    *,
    tool_call_budget: int = 1,
) -> tuple[UUID, UUID, JobClaim]:
    async with factory() as session:
        await session.execute(text("TRUNCATE TABLE jobs, artifacts CASCADE"))
        await session.commit()
        app = ProjectApplication(PostgresDomainStore(session))
        project = await app.create_project(
            name=f"Budget fixture {uuid4()}",
            goal="Prove durable budget fencing",
            idempotency_key=f"budget-project-{uuid4()}",
        )
        requirement, modules = await app.approve_requirements(
            project_id=project.id,
            expected_project_revision=project.revision,
            goal=project.goal,
            hard_constraints=("Never overspend",),
            preferences=(),
            available_resources=(),
            unknowns=(),
            modules=(
                {
                    "key": "budget",
                    "name": "Budget",
                    "responsibility": "Fence external operations",
                },
            ),
            idempotency_key=f"budget-requirements-{uuid4()}",
        )
        basis_hash = sha256(f"{requirement.id}:{modules[0].id}".encode()).hexdigest()
        runtime = PostgresRuntime(session)
        root_job_id = await runtime.create_job(
            project_id=project.id,
            kind="research_wave",
            basis_hash=basis_hash,
            basis_project_revision=project.revision + 1,
            profile_id="research-orchestrator",
            profile_revision=1,
            token_budget_cap=8_000,
            tool_call_budget_cap=tool_call_budget,
        )
        parent_claim = await runtime.claim_next_job(worker_id="budget-parent")
        assert parent_claim is not None and parent_claim.job_id == root_job_id
        delegation = DelegationSpec(
            parent_job_id=root_job_id,
            parent_attempt_id=parent_claim.attempt_id,
            parent_claim_generation=parent_claim.claim_generation,
            graph_step_id="budget.test",
            profile_id="research-worker-ro",
            profile_revision=RESEARCH_WORKER_PROFILE.revision,
            basis_hash=basis_hash,
            shard_key="only",
            idempotency_key=f"budget-delegation-{uuid4()}",
            token_budget=4_000,
            tool_call_budget=tool_call_budget,
            deadline=parent_claim.lease_expires_at,
        )
        wave = await runtime.create_delegation_wave(
            specs=(delegation,),
            policy=JoinPolicy(
                mode=JoinMode.ALL_REQUIRED,
                expected_delegation_ids=(delegation.delegation_id,),
                min_successes=1,
                deadline=delegation.deadline,
            ),
        )
        child_claim = await runtime.claim_next_job(worker_id="budget-child")
        assert child_claim is not None and child_claim.job_id == wave.child_job_ids[0]
        ledger = BudgetLedger(session)
        account_id = await ledger.get_account_id(root_job_id)
        allocation = await ledger.get_allocation(
            account_id=account_id,
            owner_kind=BudgetOwnerKind.CHILD,
            owner_ref=child_claim.job_id,
        )
        return account_id, allocation.allocation_id, child_claim


@pytest.mark.asyncio
async def test_profile_revision_is_content_addressed_immutable_and_frozen_per_job() -> None:
    factory, engine = _factory()
    try:
        next_revision_number = RESEARCH_WORKER_PROFILE.revision + 1
        next_revision = build_profile_revision(
            profile_id="research-worker-ro",
            revision=next_revision_number,
            purpose="Second worker policy used only by newly created runs.",
            prompt_template=RESEARCH_WORKER_PROFILE.prompt_template + "\nPrefer primary sources.",
            input_schema_ref=RESEARCH_WORKER_PROFILE.input_schema_ref,
            output_schema_ref=RESEARCH_WORKER_PROFILE.output_schema_ref,
            allowed_tool_classes=RESEARCH_WORKER_PROFILE.allowed_tool_classes,
            allowed_effects=RESEARCH_WORKER_PROFILE.allowed_effects,
            memory_read_scopes=RESEARCH_WORKER_PROFILE.memory_read_scopes,
            model_capabilities=RESEARCH_WORKER_PROFILE.model_capabilities,
            token_cap=4_000,
            tool_call_cap=3,
            concurrency_cap=2,
            timeout_seconds=300,
            retry_policy={"max_physical_attempts": 1, "hidden_provider_retries": 0},
            evaluator_policy={"require_structured_response": True},
        )
        async with factory() as session:
            profiles = ProfileRepository(session)
            await profiles.register(next_revision)
            await profiles.activate(next_revision.profile_id, next_revision.revision)
            await session.commit()

            conflicting = next_revision.model_copy(update={"definition_hash": "f" * 64})
            with pytest.raises(ProfileConflictError, match="different content"):
                await profiles.register(conflicting)

        root_ids: list[UUID] = []
        for expected_revision in (next_revision_number, RESEARCH_WORKER_PROFILE.revision):
            async with factory() as session:
                if expected_revision == RESEARCH_WORKER_PROFILE.revision:
                    await ProfileRepository(session).activate(
                        "research-worker-ro", RESEARCH_WORKER_PROFILE.revision
                    )
                    await session.commit()
                app = ProjectApplication(PostgresDomainStore(session))
                project = await app.create_project(
                    name=f"Profile binding {expected_revision}",
                    goal="Freeze the active profile",
                    idempotency_key=f"profile-project-{uuid4()}",
                )
                root_job_id = await PostgresRuntime(session).create_job(
                    project_id=project.id,
                    kind="research_wave",
                    basis_hash="a" * 64,
                    basis_project_revision=project.revision,
                    profile_id="research-orchestrator",
                    profile_revision=1,
                )
                root_ids.append(root_job_id)
                binding = await ProfileRepository(session).get_binding(
                    root_job_id,
                    "research-worker",
                )
                assert binding.profile_revision == expected_revision

        async with factory() as session:
            first = await ProfileRepository(session).get_binding(root_ids[0], "research-worker")
            assert first.profile_revision == next_revision_number
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(
                    text(
                        "UPDATE agent_profile_revisions SET purpose = 'changed' "
                        "WHERE profile_id = 'research-worker-ro' AND revision = :revision"
                    ),
                    {"revision": next_revision_number},
                )
                await session.commit()
            await session.rollback()
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(
                    text(
                        "DELETE FROM agent_profile_revisions "
                        "WHERE profile_id = 'research-worker-ro' AND revision = :revision"
                    ),
                    {"revision": next_revision_number},
                )
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_budget_operation_is_idempotent_and_settles_actual_usage() -> None:
    factory, engine = _factory()
    try:
        account_id, allocation_id, claim = await _create_child_allocation(factory)
        async with factory() as session:
            ledger = BudgetLedger(session)
            operation = await ledger.reserve_operation(
                allocation_id=allocation_id,
                claim=claim,
                kind=BudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=1,
                idempotency_key=f"model-operation-{uuid4()}",
                request_hash="b" * 64,
                provider="fake",
                model_or_tool="fake-model",
                reserved_tokens=1_000,
            )
            replay = await ledger.reserve_operation(
                allocation_id=allocation_id,
                claim=claim,
                kind=BudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=1,
                idempotency_key=operation.idempotency_key,
                request_hash="b" * 64,
                provider="fake",
                model_or_tool="fake-model",
                reserved_tokens=1_000,
            )
            assert replay.operation_id == operation.operation_id
            await ledger.mark_dispatched(operation.operation_id)
            settled = await ledger.settle(
                operation.operation_id,
                consumed_tokens=625,
                consumed_tool_calls=0,
                provider_request_id="provider-request-1",
            )
            settled_replay = await ledger.settle(
                operation.operation_id,
                consumed_tokens=625,
                consumed_tool_calls=0,
                provider_request_id="provider-request-1",
            )
            assert settled.state is BudgetOperationState.SETTLED
            assert settled_replay.operation_id == settled.operation_id
            allocation = await ledger.get_allocation(
                    account_id=account_id,
                owner_kind=BudgetOwnerKind.CHILD,
                owner_ref=claim.job_id,
            )
            assert allocation.token_reserved == 0
            assert allocation.token_consumed == 625
            await session.commit()
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_concurrent_tool_reservations_do_not_overspend() -> None:
    factory, engine = _factory()
    try:
        _, allocation_id, claim = await _create_child_allocation(factory, tool_call_budget=1)

        async def reserve(index: int) -> object:
            async with factory() as session:
                try:
                    operation = await BudgetLedger(session).reserve_operation(
                        allocation_id=allocation_id,
                        claim=claim,
                        kind=BudgetOperationKind.TOOL,
                        logical_step="research.web_search",
                        physical_attempt_no=index,
                        idempotency_key=f"tool-operation-{uuid4()}",
                        request_hash=str(index) * 64,
                        provider="fake-search",
                        model_or_tool="web_search",
                        reserved_tool_calls=1,
                    )
                    await session.commit()
                    return operation
                except BaseException as exc:
                    await session.rollback()
                    return exc

        results = await asyncio.gather(reserve(1), reserve(2))
        assert sum(not isinstance(item, BaseException) for item in results) == 1
        assert sum(isinstance(item, BudgetLimitExceededError) for item in results) == 1
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetOperationRow)
                    .where(BudgetOperationRow.allocation_id == allocation_id)
                )
                == 1
            )
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stale_claim_is_rejected_and_reclaim_resolves_open_operations() -> None:
    factory, engine = _factory()
    try:
        account_id, allocation_id, claim = await _create_child_allocation(factory)
        stale_claim = claim.model_copy(update={"lease_token": uuid4()})
        async with factory() as session:
            ledger = BudgetLedger(session)
            with pytest.raises(BudgetClaimStaleError, match="stale"):
                await ledger.reserve_operation(
                    allocation_id=allocation_id,
                    claim=stale_claim,
                    kind=BudgetOperationKind.MODEL,
                    logical_step="research.model",
                    physical_attempt_no=1,
                    idempotency_key=f"stale-operation-{uuid4()}",
                    request_hash="c" * 64,
                    provider="fake",
                    model_or_tool="fake-model",
                    reserved_tokens=500,
                )
            await session.rollback()

        async with factory() as session:
            ledger = BudgetLedger(session)
            operation = await ledger.reserve_operation(
                allocation_id=allocation_id,
                claim=claim,
                kind=BudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=1,
                idempotency_key=f"ambiguous-operation-{uuid4()}",
                request_hash="d" * 64,
                provider="fake",
                model_or_tool="fake-model",
                reserved_tokens=500,
            )
            await ledger.mark_dispatched(operation.operation_id)
            reserved_operation = await ledger.reserve_operation(
                allocation_id=allocation_id,
                claim=claim,
                kind=BudgetOperationKind.MODEL,
                logical_step="research.model.before_dispatch_crash",
                physical_attempt_no=2,
                idempotency_key=f"reserved-operation-{uuid4()}",
                request_hash="e" * 64,
                provider="fake",
                model_or_tool="fake-model",
                reserved_tokens=300,
            )
            await session.commit()

        async with factory() as session:
            reconciled = await BudgetLedger(session).reconcile_reclaimed_owner(
                account_id=account_id,
                owner_kind=BudgetOwnerKind.CHILD,
                owner_ref=claim.job_id,
            )
            assert reconciled is not None
            assert reconciled.status == "closed"
            assert reconciled.token_consumed == 500
            stored = await session.get(BudgetOperationRow, operation.operation_id)
            assert stored is not None
            assert stored.state == BudgetOperationState.AMBIGUOUS.value
            assert stored.consumed_tokens == 500
            released = await session.get(BudgetOperationRow, reserved_operation.operation_id)
            assert released is not None
            assert released.state == BudgetOperationState.RELEASED.value
            assert released.consumed_tokens == 0
            assert reconciled.token_reserved == 0
            await session.commit()
    finally:
        await engine.dispose()  # type: ignore[attr-defined]
