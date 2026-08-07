from __future__ import annotations

import os
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update

from aidison.agents.profiles import RESEARCH_WORKER_PROFILE
from aidison.application.service import ProjectApplication
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import PlanGapRow, PlanTaskRow
from aidison.infrastructure.planning import (
    PlanConflictError,
    PostgresPlanStore,
    build_revision_from_patch,
)
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.planning import (
    GapStatus,
    OrchestrationPlanRevision,
    PlanPatchKind,
    PlanPatchProposal,
    ResearchGap,
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


@pytest.mark.asyncio
async def test_gap_recording_is_deduplicated_and_open_list_is_scoped_to_current_revision() -> None:
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
                name="Gap dedup fixture",
                goal="Verify gap deduplication and revision scoping",
                idempotency_key=f"gap-project-{uuid4()}",
            )
            basis_hash = sha256(b"gap-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            claim = await runtime.claim_next_job(worker_id="gap-controller", lease_seconds=60)
            assert claim is not None

            initial = OrchestrationPlanRevision(
                root_job_id=str(root_job_id),
                revision=1,
                basis_hash=basis_hash,
                reason="initial",
                planner_profile_id="research-orchestrator",
                planner_profile_revision=1,
                nodes=(_node("a"), _node("b")),
            )
            store = PostgresPlanStore(session)
            await store.create_initial(claim=claim, plan=initial)

            gap = ResearchGap(
                root_job_id=root_job_id,
                task_logical_key="a",
                plan_revision=1,
                category="missing_data",
                description="No spec found for physical interface",
            )
            recorded = await store.record_gap(gap=gap)
            assert recorded.gap_hash == gap.gap_hash
            assert recorded.status == GapStatus.OPEN

            # Dedup
            same = await store.record_gap(gap=gap)
            assert same.gap_hash == gap.gap_hash

            # Different category -> different hash
            gap2 = ResearchGap(
                root_job_id=root_job_id,
                task_logical_key="a",
                plan_revision=1,
                category="compatibility_conflict",
                description="Module A and B may conflict",
            )
            recorded2 = await store.record_gap(gap=gap2)
            assert recorded2.gap_hash != gap.gap_hash

            open_gaps = await store.list_open_gaps(root_job_id=root_job_id)
            assert len(open_gaps) == 2
            assert {g.gap_hash for g in open_gaps} == {gap.gap_hash, gap2.gap_hash}

            # Resolve one
            resolved = await store.resolve_gaps(
                root_job_id=root_job_id,
                gap_hashes=(gap.gap_hash,),
                status=GapStatus.ACCEPTED,
            )
            assert resolved == 1

            open_gaps = await store.list_open_gaps(root_job_id=root_job_id)
            assert len(open_gaps) == 1
            assert open_gaps[0].gap_hash == gap2.gap_hash

            # Priority filter
            await store.record_gap(
                gap=ResearchGap(
                    root_job_id=root_job_id,
                    task_logical_key="b",
                    plan_revision=1,
                    category="test",
                    description="Low priority gap",
                    priority=5,
                )
            )
            filtered = await store.list_open_gaps(root_job_id=root_job_id, min_priority=10)
            assert len(filtered) == 0  # priority 5 falls below floor of 10

            # record_gap rejects mismatch between gap.plan_revision and current head
            with pytest.raises(PlanConflictError, match="plan_revision"):
                await store.record_gap(
                    gap=ResearchGap(
                        root_job_id=root_job_id,
                        task_logical_key="a",
                        plan_revision=99,
                        category="x",
                        description="x",
                    )
                )

            # record_gap rejects task that doesn't exist
            with pytest.raises(PlanConflictError, match="task"):
                await store.record_gap(
                    gap=ResearchGap(
                        root_job_id=root_job_id,
                        task_logical_key="nonexistent",
                        plan_revision=1,
                        category="x",
                        description="x",
                    )
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_revision2_cas_and_frontier_staleness_guard() -> None:
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
                name="Revision2 CAS fixture",
                goal="Verify revision 2+ commit, replay, and head staleness",
                idempotency_key=f"rev2-project-{uuid4()}",
            )
            basis_hash = sha256(b"rev2-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            claim = await runtime.claim_next_job(worker_id="rev2-controller", lease_seconds=60)
            assert claim is not None

            base = OrchestrationPlanRevision(
                root_job_id=str(root_job_id),
                revision=1,
                basis_hash=basis_hash,
                reason="initial",
                planner_profile_id="research-orchestrator",
                planner_profile_revision=1,
                nodes=(_node("a"),),
            )
            store = PostgresPlanStore(session)
            await store.create_initial(claim=claim, plan=base)

            # Build patch
            next_rev = build_revision_from_patch(
                base=base,
                patch_kind=PlanPatchKind.EXPAND,
                trigger="gap detected",
                new_nodes=(_node("b"),),
            )
            patch = PlanPatchProposal(
                root_job_id=str(root_job_id),
                base_revision=1,
                base_plan_hash=base.plan_hash,
                kind=PlanPatchKind.EXPAND,
                trigger="gap detected",
                payload={"gap_hash": "test"},
                new_plan=next_rev,
            )
            receipt = await store.apply_patch(claim=claim, patch=patch)
            assert receipt.new_revision == 2
            assert receipt.base_revision == 1
            current = await store.get_current(root_job_id=root_job_id)
            assert current.revision == 2
            assert current.parent_revision == 1
            assert [n.logical_key for n in current.nodes] == ["a", "b"]

            # Replay of same patch is idempotent
            replay = await store.apply_patch(claim=claim, patch=patch)
            assert replay.new_revision == receipt.new_revision
            assert replay.patch_hash == receipt.patch_hash

            # A stale patch targeting revision 1 is rejected
            stale_next = build_revision_from_patch(
                base=base,
                patch_kind=PlanPatchKind.EXPAND,
                trigger="stale attempt",
                new_nodes=(_node("c"),),
            )
            stale_patch = PlanPatchProposal(
                root_job_id=str(root_job_id),
                base_revision=1,
                base_plan_hash=base.plan_hash,
                kind=PlanPatchKind.EXPAND,
                trigger="stale attempt",
                new_plan=stale_next,
            )
            with pytest.raises(PlanConflictError, match="head is stale"):
                await store.apply_patch(claim=claim, patch=stale_patch)
            await session.rollback()

            # Frontier is after revision 2 nodes
            await session.execute(
                update(PlanTaskRow)
                .where(PlanTaskRow.logical_key == "a")
                .values(status="succeeded")
            )
            await session.commit()
            refreshed = await store.refresh_frontier(root_job_id=root_job_id)
            assert {item.logical_key for item in refreshed} == {"b"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_gaps_only_updates_open_rows_and_rejects_non_open_target() -> None:
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
                name="Resolve guard fixture",
                goal="Verify resolve_gaps only touches OPEN rows",
                idempotency_key=f"resolve-project-{uuid4()}",
            )
            basis_hash = sha256(b"resolve-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            claim = await runtime.claim_next_job(worker_id="resolve-ctrl", lease_seconds=60)
            assert claim is not None

            store = PostgresPlanStore(session)
            await store.create_initial(
                claim=claim,
                plan=OrchestrationPlanRevision(
                    root_job_id=str(root_job_id),
                    revision=1,
                    basis_hash=basis_hash,
                    reason="initial",
                    planner_profile_id="research-orchestrator",
                    planner_profile_revision=1,
                    nodes=(_node("a"),),
                ),
            )

            gap = ResearchGap(
                root_job_id=root_job_id,
                task_logical_key="a",
                plan_revision=1,
                category="missing_data",
                description="A missing spec",
            )
            await store.record_gap(gap=gap)
            await store.resolve_gaps(
                root_job_id=root_job_id,
                gap_hashes=(gap.gap_hash,),
                status=GapStatus.ACCEPTED,
            )
            # Second resolve should return 0 because the row is no longer OPEN
            resolved2 = await store.resolve_gaps(
                root_job_id=root_job_id,
                gap_hashes=(gap.gap_hash,),
                status=GapStatus.RESOLVED,
            )
            assert resolved2 == 0

            # Passing OPEN as target must be rejected
            with pytest.raises(ValueError, match="non-OPEN"):
                await store.resolve_gaps(
                    root_job_id=root_job_id,
                    gap_hashes=(gap.gap_hash,),
                    status=GapStatus.OPEN,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dedup_replay_returns_persisted_priority() -> None:
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
                name="Dedup priority fixture",
                goal="Verify dedup replay returns persisted priority not caller projection",
                idempotency_key=f"dedup-pri-{uuid4()}",
            )
            basis_hash = sha256(b"dedup-pri-basis").hexdigest()
            runtime = PostgresRuntime(session)
            root_job_id = await runtime.create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            claim = await runtime.claim_next_job(worker_id="dedup-pri-ctrl", lease_seconds=60)
            assert claim is not None

            store = PostgresPlanStore(session)
            await store.create_initial(
                claim=claim,
                plan=OrchestrationPlanRevision(
                    root_job_id=str(root_job_id),
                    revision=1,
                    basis_hash=basis_hash,
                    reason="initial",
                    planner_profile_id="research-orchestrator",
                    planner_profile_revision=1,
                    nodes=(_node("a"),),
                ),
            )

            gap = ResearchGap(
                root_job_id=root_job_id,
                task_logical_key="a",
                plan_revision=1,
                category="missing_data",
                description="A missing spec",
                priority=42,
            )
            first = await store.record_gap(gap=gap)
            assert first.priority == 42
            # Dedup replay: should return persisted canonical priority, not caller's
            replay_gap = ResearchGap(
                root_job_id=root_job_id,
                task_logical_key="a",
                plan_revision=1,
                category="missing_data",
                description="A missing spec",
                priority=99,
            )
            replay = await store.record_gap(gap=replay_gap)
            assert replay.priority == 42
            # Verify the persisted row also has priority=42
            persisted_row = await session.scalar(
                select(PlanGapRow).where(
                    PlanGapRow.root_job_id == root_job_id,
                    PlanGapRow.gap_hash == gap.gap_hash,
                )
            )
            assert persisted_row is not None and persisted_row.priority == 42
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_build_revision_from_patch_drops_dangling_edges_on_retire() -> None:
    """Retiring a node drops its edges and normalises depth across surviving edge kinds."""
    base = OrchestrationPlanRevision(
        root_job_id="fake-job",
        revision=1,
        basis_hash=sha256(b"retire-edge-basis").hexdigest(),
        reason="initial",
        planner_profile_id="research-orchestrator",
        planner_profile_revision=1,
        nodes=(
            _node("a", depth=0),
            _node("b", depth=1),
            _node("c", depth=2),
        ),
        edges=(
            TaskEdge(from_key="a", to_key="b", kind=TaskEdgeKind.DEPENDS_ON),
            TaskEdge(from_key="b", to_key="c", kind=TaskEdgeKind.DEPENDS_ON),
            TaskEdge(from_key="a", to_key="c", kind=TaskEdgeKind.EVIDENCE_FROM),
        ),
    )
    # Retire "b": its two edges drop while surviving a→c keeps c at depth 1.
    next_rev = build_revision_from_patch(
        base=base,
        patch_kind=PlanPatchKind.CONTRACT,
        trigger="retire b",
        retired_keys=("b",),
    )
    assert {n.logical_key for n in next_rev.nodes} == {"a", "c"}
    assert next_rev.edges == (
        TaskEdge(from_key="a", to_key="c", kind=TaskEdgeKind.EVIDENCE_FROM),
    )
    remaining = {n.logical_key: n.depth for n in next_rev.nodes}
    assert remaining["a"] == 0
    assert remaining["c"] == 1  # surviving EVIDENCE_FROM edge still contributes graph depth
