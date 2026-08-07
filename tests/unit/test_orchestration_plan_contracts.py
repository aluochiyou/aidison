from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from aidison.runtime.planning import (
    OrchestrationPlanRevision,
    PlanPatchKind,
    PlanPatchProposal,
    ResearchMode,
    TaskEdge,
    TaskEdgeKind,
    TaskNode,
    TaskStatus,
    build_fixed_research_shadow_plan,
    build_research_shadow_plan,
    canonical_patch_hash,
    canonical_plan_hash,
)


def _node(key: str, *, depth: int = 0) -> TaskNode:
    return TaskNode(
        logical_key=key,
        objective=f"核验 {key}",
        mode=ResearchMode.ATOM,
        role_key="research-worker",
        profile_id="research-worker-ro",
        profile_revision=4,
        budget_ref="budget://account-1",
        depth=depth,
        input_refs=(f"module://{key}",),
        success_criteria=("至少一条可追溯证据",),
        stop_criteria=("达到本节点证据要求",),
    )


def test_fixed_wave_shadow_plan_is_deterministic_and_preserves_current_shards() -> None:
    modules = [
        SimpleNamespace(id="module-1", key="structure"),
        SimpleNamespace(id="module-2", key="power"),
        SimpleNamespace(id="module-3", key="control"),
    ]

    first = build_fixed_research_shadow_plan(
        root_job_id="job-1",
        basis_hash="a" * 64,
        modules=modules,
        profile_id="research-worker-ro",
        profile_revision=4,
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        budget_ref="budget://account-1",
    )
    replay = build_fixed_research_shadow_plan(
        root_job_id="job-1",
        basis_hash="a" * 64,
        modules=modules,
        profile_id="research-worker-ro",
        profile_revision=4,
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        budget_ref="budget://account-1",
    )

    assert first.revision == 1
    assert [item.logical_key for item in first.nodes] == ["research.shard-1", "research.shard-2"]
    assert first.nodes[0].input_refs == ("module://module-1", "module://module-3")
    assert first.nodes[1].input_refs == ("module://module-2",)
    assert first.nodes[0].status == "planned"
    assert first.plan_hash == canonical_plan_hash(first)
    assert canonical_plan_hash(first) == canonical_plan_hash(replay)


def test_nway_shadow_plan_stably_partitions_ten_modules_into_eight_shards() -> None:
    modules = [
        SimpleNamespace(id=f"module-{index}", key=f"module-{index}")
        for index in range(10)
    ]

    plan = build_research_shadow_plan(
        root_job_id="job-nway",
        basis_hash="b" * 64,
        modules=modules,
        profile_id="research-worker-ro",
        profile_revision=5,
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        budget_ref="budget://account-nway",
        max_shards=8,
    )

    assert [item.logical_key for item in plan.nodes] == [
        f"research.shard-{index}" for index in range(1, 9)
    ]
    assert plan.nodes[0].input_refs == ("module://module-0", "module://module-8")
    assert plan.nodes[1].input_refs == ("module://module-1", "module://module-9")
    assert all(node.input_refs for node in plan.nodes)


def test_plan_rejects_duplicate_keys_and_unknown_edges() -> None:
    with pytest.raises(ValidationError, match="logical_key"):
        OrchestrationPlanRevision(
            root_job_id="job-1",
            revision=1,
            basis_hash="b" * 64,
            reason="shadow",
            planner_profile_id="planner-ro",
            planner_profile_revision=2,
            nodes=(_node("a"), _node("a")),
        )

    with pytest.raises(ValidationError, match="unknown task node"):
        OrchestrationPlanRevision(
            root_job_id="job-1",
            revision=1,
            basis_hash="b" * 64,
            reason="shadow",
            planner_profile_id="planner-ro",
            planner_profile_revision=2,
            nodes=(_node("a"),),
            edges=(TaskEdge(from_key="a", to_key="missing", kind=TaskEdgeKind.DEPENDS_ON),),
        )


def test_plan_rejects_cycles_and_excessive_depth() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        OrchestrationPlanRevision(
            root_job_id="job-1",
            revision=1,
            basis_hash="c" * 64,
            reason="invalid cycle",
            planner_profile_id="planner-ro",
            planner_profile_revision=2,
            nodes=(_node("a"), _node("b")),
            edges=(
                TaskEdge(from_key="a", to_key="b", kind=TaskEdgeKind.DEPENDS_ON),
                TaskEdge(from_key="b", to_key="a", kind=TaskEdgeKind.DEPENDS_ON),
            ),
        )

    with pytest.raises(ValidationError, match="depth"):
        _node("too-deep", depth=5)

    chain = tuple(_node(chr(ord("a") + index)) for index in range(6))
    edges = tuple(
        TaskEdge(
            from_key=chain[index].logical_key,
            to_key=chain[index + 1].logical_key,
            kind=TaskEdgeKind.DEPENDS_ON,
        )
        for index in range(5)
    )
    with pytest.raises(ValidationError, match="dependency depth"):
        OrchestrationPlanRevision(
            root_job_id="job-1",
            revision=1,
            basis_hash="d" * 64,
            reason="chain too deep",
            planner_profile_id="planner-ro",
            planner_profile_revision=2,
            nodes=chain,
            edges=edges,
        )


def test_plan_rejects_declared_depth_that_disagrees_with_dependencies() -> None:
    with pytest.raises(ValidationError, match="depth must match"):
        OrchestrationPlanRevision(
            root_job_id="job-1",
            revision=1,
            basis_hash="e" * 64,
            reason="invalid declared depth",
            planner_profile_id="planner-ro",
            planner_profile_revision=2,
            nodes=(_node("a"), _node("b")),
            edges=(TaskEdge(from_key="a", to_key="b", kind=TaskEdgeKind.DEPENDS_ON),),
        )


def test_plan_rejects_noncanonical_plan_hash() -> None:
    with pytest.raises(ValidationError, match="plan_hash"):
        OrchestrationPlanRevision(
            root_job_id="job-1",
            revision=1,
            basis_hash="f" * 64,
            reason="invalid hash",
            planner_profile_id="planner-ro",
            planner_profile_revision=2,
            plan_hash="0" * 64,
            nodes=(_node("a"),),
        )


def test_plan_hash_is_stable_when_equivalent_nodes_and_edges_are_reordered() -> None:
    first = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="a" * 64,
        reason="stable ordering",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"), _node("b", depth=1)),
        edges=(TaskEdge(from_key="a", to_key="b", kind=TaskEdgeKind.DEPENDS_ON),),
    )
    reordered = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="a" * 64,
        reason="stable ordering",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("b", depth=1), _node("a")),
        edges=(TaskEdge(from_key="a", to_key="b", kind=TaskEdgeKind.DEPENDS_ON),),
    )

    assert first.plan_hash == reordered.plan_hash

    completed = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="a" * 64,
        reason="stable ordering",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(
            _node("a").model_copy(update={"status": TaskStatus.SUCCEEDED}),
            _node("b", depth=1),
        ),
        edges=(TaskEdge(from_key="a", to_key="b", kind=TaskEdgeKind.DEPENDS_ON),),
    )
    assert first.plan_hash == completed.plan_hash


def test_patch_binds_the_next_revision_to_its_base_hash() -> None:
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="a" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"),),
    )
    next_plan = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=2,
        parent_revision=1,
        basis_hash="a" * 64,
        reason="evidence gap",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"), _node("b")),
    )
    patch = PlanPatchProposal(
        root_job_id="job-1",
        base_revision=1,
        base_plan_hash=base.plan_hash,
        kind=PlanPatchKind.EXPAND,
        trigger="new compatibility gap",
        payload={"gap_ref": "gap://1"},
        new_plan=next_plan,
    )

    assert patch.patch_hash == canonical_patch_hash(patch)
    with pytest.raises(ValidationError, match="advance exactly"):
        PlanPatchProposal(
            root_job_id="job-1",
            base_revision=1,
            base_plan_hash=base.plan_hash,
            kind=PlanPatchKind.EXPAND,
            trigger="invalid jump",
            new_plan=OrchestrationPlanRevision(
                root_job_id="job-1",
                revision=3,
                parent_revision=1,
                basis_hash="a" * 64,
                reason="invalid jump",
                planner_profile_id="planner-ro",
                planner_profile_revision=2,
                nodes=(_node("a"),),
            ),
        )
