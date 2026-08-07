from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.agents.profiles import RESEARCH_WORKER_PROFILE
from aidison.application.execution import DurablePlanExecutor, PlannedDelegation
from aidison.application.service import ProjectApplication
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.orm import (
    BudgetAllocationRow,
    DelegationRow,
    JobRow,
    JoinGroupRow,
    PlanHeadRow,
)
from aidison.infrastructure.planning import PlanConflictError, PostgresPlanStore
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import (
    DelegationSpec,
    JobClaim,
    JoinMode,
    JoinPolicy,
    RuntimeWorkItem,
)
from aidison.runtime.planning import OrchestrationPlanRevision, TaskNode

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_plan_wave_rolls_back_as_one_transaction_when_a_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                name="Atomic durable plan fixture",
                goal="Roll back plan, wave, budget, and bindings together",
                idempotency_key=f"atomic-plan-project-{uuid4()}",
            )
            basis_hash = sha256(b"atomic-plan").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
                token_budget_cap=4_000,
                tool_call_budget_cap=4,
            )
            claim = await runtime.claim_next_job(worker_id="atomic-plan-parent")
            assert claim is not None

        nodes = tuple(
            TaskNode(
                logical_key=f"research.atomic-{index}",
                objective=f"Atomic task {index}",
                mode="atom",
                role_key="research-worker",
                profile_id=RESEARCH_WORKER_PROFILE.profile_id,
                profile_revision=RESEARCH_WORKER_PROFILE.revision,
                budget_ref=f"budget://job/{root_job_id}",
                input_refs=(f"module://atomic-{index}",),
                success_criteria=("one result",),
                stop_criteria=("task complete",),
                depth=0,
            )
            for index in range(2)
        )
        plan = OrchestrationPlanRevision(
            root_job_id=str(root_job_id),
            revision=1,
            basis_hash=basis_hash,
            reason="atomic dispatch test",
            planner_profile_id=claim.profile_id,
            planner_profile_revision=claim.profile_revision,
            nodes=nodes,
        )
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        specs = tuple(
            DelegationSpec(
                delegation_id=uuid5(claim.attempt_id, f"atomic:{node.logical_key}"),
                parent_job_id=root_job_id,
                parent_attempt_id=claim.attempt_id,
                parent_claim_generation=claim.claim_generation,
                graph_step_id="research.atomic",
                role_key=node.role_key,
                profile_id=node.profile_id,
                profile_revision=node.profile_revision,
                basis_hash=basis_hash,
                shard_key=node.logical_key,
                idempotency_key=f"{claim.attempt_id}:atomic:{node.logical_key}",
                input_refs=node.input_refs,
                token_budget=1_000,
                tool_call_budget=1,
                deadline=deadline,
            )
            for node in nodes
        )
        policy = JoinPolicy(
            mode=JoinMode.ALL_REQUIRED,
            expected_delegation_ids=tuple(spec.delegation_id for spec in specs),
            min_successes=2,
            deadline=deadline,
        )

        original_bind = PostgresPlanStore.bind_task_job
        bind_count = 0

        async def fail_second_bind(self: PostgresPlanStore, **kwargs: Any) -> None:
            nonlocal bind_count
            bind_count += 1
            if bind_count == 2:
                raise PlanConflictError("injected second binding failure")
            await original_bind(self, **kwargs)

        monkeypatch.setattr(PostgresPlanStore, "bind_task_job", fail_second_bind)
        executor = DurablePlanExecutor(
            session_factory=factory,
            join_waiter=cast(Any, object()),
        )
        with pytest.raises(PlanConflictError, match="injected second binding failure"):
            await executor.dispatch_ready_wave(
                work=await _load_work(factory, claim),
                delegations=tuple(
                    PlannedDelegation(task_logical_key=node.logical_key, spec=spec)
                    for node, spec in zip(nodes, specs, strict=True)
                ),
                policy=policy,
                initial_plan=plan,
            )

        async with factory() as session:
            assert await session.get(PlanHeadRow, root_job_id) is None
            assert await session.scalar(select(func.count()).select_from(JoinGroupRow)) == 0
            assert await session.scalar(select(func.count()).select_from(DelegationRow)) == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(JobRow)
                    .where(JobRow.parent_job_id == root_job_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BudgetAllocationRow)
                    .where(BudgetAllocationRow.owner_kind == "child")
                )
                == 0
            )
    finally:
        await engine.dispose()


async def _load_work(
    factory: async_sessionmaker[AsyncSession],
    claim: JobClaim,
) -> RuntimeWorkItem:
    async with factory() as session:
        return await PostgresRuntime(session).load_work_item(claim)
