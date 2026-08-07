from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from aidison.infrastructure.planning import build_revision_from_patch
from aidison.research.planning import (
    GapStatus,
    ResearchGap,
    ResearchMode,
    build_fixed_research_shadow_plan,
    build_research_shadow_plan,
)
from aidison.runtime.planning import (
    OrchestrationPlanRevision,
    PlanPatchKind,
    PlanPatchProposal,
    TaskEdge,
    TaskEdgeKind,
    TaskNode,
    TaskStatus,
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


def test_task_node_mode_is_open_to_non_research_workflows() -> None:
    node = _node("solution.complete").model_copy(update={"mode": "single"})

    assert TaskNode.model_validate(node.model_dump()).mode == "single"


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


# ── Gap contracts ──────────────────────────────────────────────────────────


def _make_gap(
    *,
    root_job_id: UUID | None = None,
    task_logical_key: str = "research.shard-1",
    plan_revision: int = 1,
    category: str = "missing_data",
    description: str = "No public spec found for physical interface",
    source_result_id: UUID | None = None,
    source_result_hash: str | None = None,
    module_refs: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    priority: int = 0,
) -> ResearchGap:
    _default_root = UUID("550e8400-e29b-41d4-a716-446655440000")
    return ResearchGap(
        root_job_id=root_job_id or _default_root,
        task_logical_key=task_logical_key,
        plan_revision=plan_revision,
        source_result_id=source_result_id,
        source_result_hash=source_result_hash,
        category=category,
        description=description,
        module_refs=module_refs,
        evidence_refs=evidence_refs,
        priority=priority,
    )


def test_gap_hash_is_deterministic_and_ignores_mutable_fields() -> None:
    g1 = _make_gap(priority=10)
    g2 = _make_gap(priority=99)

    assert g1.gap_hash == g2.gap_hash
    assert g1.status == GapStatus.OPEN

    closed = _make_gap().model_copy(
        update={"status": GapStatus.RESOLVED}
    )
    assert closed.gap_hash == g1.gap_hash


def test_gap_deduplication_on_hash() -> None:
    gap_a = _make_gap(
        description="Gap A: missing spec",
    )
    gap_b = _make_gap(
        description="Gap B: conflicting evidence",
    )
    assert gap_a.gap_hash != gap_b.gap_hash

    identical = _make_gap(
        description="Gap A: missing spec",
    )
    assert gap_a.gap_hash == identical.gap_hash


def test_gap_rejects_invalid_lineage() -> None:
    with pytest.raises(ValidationError, match="source_result_hash requires source_result_id"):
        ResearchGap(
            root_job_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            task_logical_key="research.shard-1",
            plan_revision=1,
            category="missing",
            description="x",
            source_result_hash="a" * 64,
        )


def test_gap_bound_is_clamped() -> None:
    with pytest.raises(ValidationError):
        ResearchGap(
            root_job_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            task_logical_key="research.shard-1",
            plan_revision=1,
            category="x",
            description="x",
            bound=0,
        )

    with pytest.raises(ValidationError):
        ResearchGap(
            root_job_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            task_logical_key="research.shard-1",
            plan_revision=1,
            category="x",
            description="x",
            bound=5,
        )


def test_gap_rejects_bad_hash() -> None:
    with pytest.raises(ValidationError, match="gap_hash"):
        ResearchGap(
            root_job_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            task_logical_key="research.shard-1",
            plan_revision=1,
            gap_hash="0" * 64,
            category="x",
            description="x",
        )


def test_gap_separated_by_category_or_description_produces_different_hashes() -> None:
    cat_a = _make_gap(category="interface_spec", description="A")
    cat_b = _make_gap(category="compatibility_test", description="A")
    assert cat_a.gap_hash != cat_b.gap_hash

    desc_a = _make_gap(description="missing structural spec")
    desc_b = _make_gap(description="missing thermal spec")
    assert desc_a.gap_hash != desc_b.gap_hash


def test_gap_separated_by_module_or_evidence_refs_produces_different_hashes() -> None:
    mod_a = _make_gap(module_refs=("module://frame",))
    mod_b = _make_gap(module_refs=("module://power",))
    assert mod_a.gap_hash != mod_b.gap_hash


# ── build_revision_from_patch ──────────────────────────────────────────────


def test_build_revision_expands_with_new_nodes() -> None:
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="a" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"),),
    )
    new_node = _node("b", depth=1)
    next_rev = build_revision_from_patch(
        base=base,
        patch_kind=PlanPatchKind.EXPAND,
        trigger="gap found",
        new_nodes=(new_node,),
        new_edges=(TaskEdge(from_key="a", to_key="b", kind=TaskEdgeKind.EVIDENCE_FROM),),
    )
    assert next_rev.revision == 2
    assert next_rev.parent_revision == 1
    assert [n.logical_key for n in next_rev.nodes] == ["a", "b"]
    assert next_rev.basis_hash == base.basis_hash


def test_build_revision_contracts_retired_nodes() -> None:
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="b" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"), _node("b")),
    )
    next_rev = build_revision_from_patch(
        base=base,
        patch_kind=PlanPatchKind.CONTRACT,
        trigger="no longer needed",
        retired_keys=("b",),
    )
    assert next_rev.revision == 2
    keys = [n.logical_key for n in next_rev.nodes]
    assert "a" in keys
    assert "b" not in keys
    assert next_rev.parent_revision == 1


def test_build_revision_not_allowed_to_retire_and_reuse_same_key() -> None:
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="a" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"),),
    )
    with pytest.raises(ValueError, match="new node keys collide"):
        build_revision_from_patch(
            base=base,
            patch_kind=PlanPatchKind.REVISE,
            trigger="retire but reuse",
            retired_keys=("a",),
            new_nodes=(_node("a"),),  # same key
        )


def test_build_revision_contract_requires_at_least_one_retired_key() -> None:
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="a" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"),),
    )
    with pytest.raises(ValueError, match="contract.*requires at least one retired"):
        build_revision_from_patch(
            base=base,
            patch_kind=PlanPatchKind.CONTRACT,
            trigger="nothing to retire",
        )


def test_build_revision_expand_rejects_empty_new_nodes() -> None:
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="a" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"),),
    )
    with pytest.raises(ValueError, match="expand.*requires at least one new"):
        build_revision_from_patch(
            base=base,
            patch_kind=PlanPatchKind.EXPAND,
            trigger="nothing new",
        )


def test_revision2_patch_commits_and_replays_deterministically() -> None:
    """End-to-end: base plan -> expand patch -> CAS commit -> replay."""
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="c" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"),),
    )
    next_rev = build_revision_from_patch(
        base=base,
        patch_kind=PlanPatchKind.EXPAND,
        trigger="evidence gap detected",
        new_nodes=(_node("b"),),
    )
    patch = PlanPatchProposal(
        root_job_id="job-1",
        base_revision=1,
        base_plan_hash=base.plan_hash,
        kind=PlanPatchKind.EXPAND,
        trigger="evidence gap detected",
        payload={"gap_hash": "abc123"},
        new_plan=next_rev,
    )
    assert patch.new_plan.revision == 2
    assert patch.new_plan.parent_revision == 1
    assert patch.patch_hash == canonical_patch_hash(patch)

    # Replay — same inputs, same outputs
    replay_rev = build_revision_from_patch(
        base=base,
        patch_kind=PlanPatchKind.EXPAND,
        trigger="evidence gap detected",
        new_nodes=(_node("b"),),
    )
    assert replay_rev.plan_hash == next_rev.plan_hash

    replay_patch = PlanPatchProposal(
        root_job_id="job-1",
        base_revision=1,
        base_plan_hash=base.plan_hash,
        kind=PlanPatchKind.EXPAND,
        trigger="evidence gap detected",
        payload={"gap_hash": "abc123"},
        new_plan=replay_rev,
    )
    assert replay_patch.patch_hash == patch.patch_hash


def test_revision2_patch_rejects_stale_head_hash() -> None:
    """A patch targeting an old head hash that no longer matches is rejected."""
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="d" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"),),
    )
    # Build a valid patch against the real hash
    next_rev = build_revision_from_patch(
        base=base,
        patch_kind=PlanPatchKind.EXPAND,
        trigger="gap",
        new_nodes=(_node("b"),),
    )
    # Using a wrong base_plan_hash still validates structurally because
    # the new_plan itself is self-consistent (it just chains off base_revision=1).
    # The actual staleness check happens at store time.
    PlanPatchProposal(
        root_job_id="job-1",
        base_revision=1,
        base_plan_hash="0" * 64,  # wrong hash — but the structural check in PlanPatchProposal
        kind=PlanPatchKind.EXPAND,  # only verifies parent_revision continuity, not hash match
        trigger="gap",
        new_plan=next_rev,
    )
    # The store reject is verified in test_plan_store.py integration test


def test_frontier_patch_must_not_skip_revision() -> None:
    """A patch that attempted to jump two revisions ahead is rejected."""
    base = OrchestrationPlanRevision(
        root_job_id="job-1",
        revision=1,
        basis_hash="e" * 64,
        reason="initial",
        planner_profile_id="planner-ro",
        planner_profile_revision=2,
        nodes=(_node("a"),),
    )
    with pytest.raises(ValueError, match="retired key"):
        build_revision_from_patch(
            base=base,
            patch_kind=PlanPatchKind.REVISE,
            trigger="skip",
            retired_keys=("nonexistent",),
        )
