from __future__ import annotations

from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from aidison.domain.models import SolutionDependency, SolutionDependencyKind, SolutionVersion
from aidison.solution.contracts import SolutionChangeKind, SolutionChangeSet
from aidison.solution.impact import (
    ImpactDisposition,
    build_impact_change_plan,
    build_steering_preview,
    traverse_solution_dependencies,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _solution() -> tuple[SolutionVersion, tuple[UUID, UUID, UUID, UUID]]:
    project_id = uuid4()
    module_a, module_b, module_c, module_d = (uuid4(), uuid4(), uuid4(), uuid4())
    solution = SolutionVersion(
        project_id=project_id,
        version=1,
        requirement_revision_id=uuid4(),
        basis_hash=_hash("solution-basis"),
        module_snapshots=tuple(
            {"module_id": str(item)} for item in (module_a, module_b, module_c, module_d)
        ),
        dependencies=(
            # Deliberately reverse order: traversal must not depend on input order.
            SolutionDependency(
                producer_module_id=module_c,
                consumer_module_id=module_d,
                kind=SolutionDependencyKind.DATA,
                interface_key="c-to-d",
                evidence_refs=("evidence-binding://c",),
            ),
            SolutionDependency(
                producer_module_id=module_a,
                consumer_module_id=module_c,
                kind=SolutionDependencyKind.ELECTRICAL,
                interface_key="a-to-c",
                evidence_refs=("evidence-binding://ac",),
            ),
            SolutionDependency(
                producer_module_id=module_a,
                consumer_module_id=module_b,
                kind=SolutionDependencyKind.MECHANICAL,
                interface_key="a-to-b",
                evidence_refs=("evidence-binding://ab",),
            ),
        ),
        approved_decision_id=uuid4(),
    )
    return solution, (module_a, module_b, module_c, module_d)


def _change_set(
    solution: SolutionVersion,
    changed_module_id: UUID,
    kind: SolutionChangeKind = SolutionChangeKind.INTERFACE,
) -> SolutionChangeSet:
    return SolutionChangeSet(
        change_set_id=uuid4(),
        project_id=solution.project_id,
        base_solution_version_id=solution.id,
        base_solution_basis_hash=solution.basis_hash,
        changed_module_ids=(changed_module_id,),
        change_kinds=(kind,),
        trigger_ref="observation://fixture",
    )


def _change_set_of_kind(
    solution: SolutionVersion,
    changed_module_id: UUID,
    kind: SolutionChangeKind,
) -> SolutionChangeSet:
    return _change_set(solution, changed_module_id, kind)


def test_typed_dependency_traversal_is_directional_and_traceable() -> None:
    solution, (module_a, module_b, module_c, module_d) = _solution()

    partition = traverse_solution_dependencies(
        change_set=_change_set(solution, module_a),
        solution=solution,
    )

    assert partition.direct_module_ids == (module_a,)
    assert partition.transitive_module_ids == (module_b, module_c, module_d)
    assert partition.affected_module_ids == (module_a, module_b, module_c, module_d)
    assert partition.unaffected_module_ids == ()
    by_module = {path.affected_module_id: path for path in partition.paths}
    assert by_module[module_d].module_path == (module_a, module_c, module_d)
    assert by_module[module_d].interface_keys == ("a-to-c", "c-to-d")
    assert by_module[module_d].evidence_refs == (
        "evidence-binding://ac",
        "evidence-binding://c",
    )


def test_typed_dependency_traversal_does_not_propagate_upstream() -> None:
    solution, (module_a, module_b, module_c, module_d) = _solution()

    partition = traverse_solution_dependencies(
        change_set=_change_set(solution, module_d),
        solution=solution,
    )

    assert partition.direct_module_ids == (module_d,)
    assert partition.transitive_module_ids == ()
    assert partition.unaffected_module_ids == (module_a, module_b, module_c)


def test_typed_dependency_traversal_rejects_foreign_change_module() -> None:
    solution, _ = _solution()

    with pytest.raises(ValueError, match="outside its solution version"):
        traverse_solution_dependencies(
            change_set=_change_set(solution, uuid4()),
            solution=solution,
        )


def test_change_plan_keeps_evidence_revalidation_inside_structural_frontier() -> None:
    solution, (module_a, module_b, module_c, module_d) = _solution()
    change_set = _change_set_of_kind(solution, module_a, SolutionChangeKind.EVIDENCE)
    partition = traverse_solution_dependencies(change_set=change_set, solution=solution)

    plan = build_impact_change_plan(change_set=change_set, partition=partition)
    preview = build_steering_preview(plan)

    by_module = {item.module_id: item for item in plan.classifications}
    assert by_module[module_a].disposition is ImpactDisposition.REVALIDATE
    assert by_module[module_d].disposition is ImpactDisposition.REVERIFY
    assert by_module[module_d].dependency_path is not None
    assert plan.semantic_analysis_module_ids == ()
    assert preview.affected_module_ids == (module_a, module_b, module_c, module_d)
    assert preview.required_dispositions == (
        ImpactDisposition.REVALIDATE,
        ImpactDisposition.REVERIFY,
    )
    assert preview.semantic_analysis_required is False


def test_change_plan_marks_interface_change_for_bounded_semantic_analysis() -> None:
    solution, (module_a, module_b, module_c, module_d) = _solution()
    change_set = _change_set_of_kind(solution, module_a, SolutionChangeKind.INTERFACE)
    partition = traverse_solution_dependencies(change_set=change_set, solution=solution)

    plan = build_impact_change_plan(change_set=change_set, partition=partition)

    assert all(item.disposition is ImpactDisposition.REVERIFY for item in plan.classifications)
    assert plan.semantic_analysis_module_ids == (module_a, module_b, module_c, module_d)
