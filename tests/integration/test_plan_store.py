from __future__ import annotations

import os
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text, update

from aidison.agents.profiles import RESEARCH_WORKER_PROFILE
from aidison.application.service import ProjectApplication
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import PlanTaskRow
from aidison.infrastructure.planning import PlanConflictError, PostgresPlanStore
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.planning import (
    OrchestrationPlanRevision,
    PlanPatchKind,
    PlanPatchProposal,
    ResearchMode,
    TaskEdge,
    TaskEdgeKind,
    TaskNode,
)

pytestmark = pytest.mark.integration


def _node(key: str, *, depth: int = 0) -> TaskNode:
    return TaskNode(
        logical_key=key,
        objective=f"Research {key}",
        mode=ResearchMode.ATOM,
        role_key="research-worker",
        profile_id=RESEARCH_WORKER_PROFILE.profile_id,
        profile_revision=RESEARCH_WORKER_PROFILE.revision,
        budget_ref="budget://root",
        depth=depth,
        input_refs=(f"module://{key}",),
        success_criteria=("one evidence-backed result",),
        stop_criteria=("the assigned evidence boundary is reached",),
    )


@pytest.mark.asyncio
async def test_plan_history_is_immutable_and_frontier_and_patch_replay_are_deterministic() -> None:
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
                name="Durable plan fixture",
                goal="Verify immutable plan revisions and a dependency frontier",
                idempotency_key=f"plan-project-{uuid4()}",
            )
            basis_hash = sha256(b"plan-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            claim = await runtime.claim_next_job(worker_id="plan-controller", lease_seconds=60)
            assert claim is not None

            initial = OrchestrationPlanRevision(
                root_job_id=str(root_job_id),
                revision=1,
                basis_hash=basis_hash,
                reason="initial bounded research plan",
                planner_profile_id="research-orchestrator",
                planner_profile_revision=1,
                nodes=(_node("discover"), _node("verify", depth=1)),
                edges=(
                    TaskEdge(
                        from_key="discover",
                        to_key="verify",
                        kind=TaskEdgeKind.DEPENDS_ON,
                    ),
                ),
            )
            store = PostgresPlanStore(session)
            assert await store.create_initial(claim=claim, plan=initial) == initial
            assert await store.create_initial(claim=claim, plan=initial) == initial
            frontier = await store.list_ready_frontier(root_job_id=root_job_id)
            assert [item.logical_key for item in frontier] == ["discover"]

            await session.execute(
                update(PlanTaskRow)
                .where(PlanTaskRow.logical_key == "discover")
                .values(status="succeeded")
            )
            await session.commit()
            refreshed = await store.refresh_frontier(root_job_id=root_job_id)
            assert [item.logical_key for item in refreshed] == ["verify"]
            frontier = await store.list_ready_frontier(root_job_id=root_job_id)
            assert [item.logical_key for item in frontier] == ["verify"]

            await session.execute(
                update(PlanTaskRow)
                .where(PlanTaskRow.logical_key == "discover")
                .values(status="failed")
            )
            await session.commit()
            assert await store.refresh_frontier(root_job_id=root_job_id) == ()
            assert (await store.get_current(root_job_id=root_job_id)).plan_hash == initial.plan_hash

            replacement = OrchestrationPlanRevision(
                root_job_id=str(root_job_id),
                revision=2,
                parent_revision=1,
                basis_hash=basis_hash,
                reason="evidence exposed a compatibility gap",
                planner_profile_id="research-orchestrator",
                planner_profile_revision=1,
                nodes=(_node("compatibility"),),
            )
            patch = PlanPatchProposal(
                root_job_id=str(root_job_id),
                base_revision=1,
                base_plan_hash=initial.plan_hash,
                kind=PlanPatchKind.REVISE,
                trigger="compatibility gap",
                payload={"gap_ref": "gap://compatibility"},
                new_plan=replacement,
            )
            receipt = await store.apply_patch(claim=claim, patch=patch)
            assert receipt.new_revision == 2
            assert await store.apply_patch(claim=claim, patch=patch) == receipt
            current = await store.get_current(root_job_id=root_job_id)
            assert current.plan_hash == replacement.plan_hash

            conflicting = PlanPatchProposal(
                root_job_id=str(root_job_id),
                base_revision=1,
                base_plan_hash=initial.plan_hash,
                kind=PlanPatchKind.EXPAND,
                trigger="stale sibling patch",
                new_plan=OrchestrationPlanRevision(
                    root_job_id=str(root_job_id),
                    revision=2,
                    parent_revision=1,
                    basis_hash=basis_hash,
                    reason="stale sibling patch",
                    planner_profile_id="research-orchestrator",
                    planner_profile_revision=1,
                    nodes=(_node("other"),),
                ),
            )
            with pytest.raises(PlanConflictError, match="head is stale"):
                await store.apply_patch(claim=claim, patch=conflicting)
            await session.rollback()
    finally:
        await engine.dispose()
