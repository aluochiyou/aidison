"""Deterministic structural frontier for a version-pinned solution change."""

from __future__ import annotations

from collections import defaultdict, deque
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aidison.domain.models import (
    ImpactClassification,
    ImpactDependencyPath,
    ImpactDisposition,
    SolutionDependency,
    SolutionVersion,
)
from aidison.solution.contracts import SolutionChangeKind, SolutionChangeSet


class ImpactChangePlan(BaseModel):
    """A reviewable plan projection; it cannot itself mutate project facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_set_id: UUID
    base_solution_version_id: UUID
    classifications: tuple[ImpactClassification, ...]
    unchanged_module_ids: tuple[UUID, ...]
    semantic_analysis_module_ids: tuple[UUID, ...]


class SteeringPreview(BaseModel):
    """The compact user-facing summary before an Impact decision is wired."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_set_id: UUID
    affected_module_ids: tuple[UUID, ...]
    unchanged_module_ids: tuple[UUID, ...]
    required_dispositions: tuple[ImpactDisposition, ...]
    semantic_analysis_required: bool


class StructuralImpactPartition(BaseModel):
    """A deterministic, evidence-traceable partition before semantic analysis.

    This is deliberately not an Impact classification.  R6-06 maps the
    structural frontier to actions such as ``REVERIFY`` or ``RESEARCH`` only
    after it has the relevant change semantics.  Keeping both stages separate
    prevents a traversal order or an LLM judgement from changing the graph.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_set_id: UUID
    direct_module_ids: tuple[UUID, ...]
    transitive_module_ids: tuple[UUID, ...]
    affected_module_ids: tuple[UUID, ...]
    unaffected_module_ids: tuple[UUID, ...]
    paths: tuple[ImpactDependencyPath, ...]


def traverse_solution_dependencies(
    *,
    change_set: SolutionChangeSet,
    solution: SolutionVersion,
) -> StructuralImpactPartition:
    """Traverse frozen producer→consumer dependency edges deterministically.

    A change to a producer can affect each consumer depending on its interface.
    The function reads no live project/module state.  It selects one stable
    shortest path per affected module for compact traceability; semantic or
    alternative-path analysis is intentionally deferred to the ImpactGraph's
    bounded analyst stage.
    """

    if change_set.project_id != solution.project_id:
        raise ValueError("change set project does not match solution version")
    if change_set.base_solution_version_id != solution.id:
        raise ValueError("change set base solution does not match solution version")
    if change_set.base_solution_basis_hash != solution.basis_hash:
        raise ValueError("change set basis does not match solution version")

    module_ids = tuple(UUID(str(snapshot["module_id"])) for snapshot in solution.module_snapshots)
    if len(module_ids) != len(set(module_ids)):
        raise ValueError("solution version module snapshots must have unique module IDs")
    module_set = set(module_ids)
    direct_set = set(change_set.changed_module_ids)
    if not direct_set <= module_set:
        raise ValueError("change set references modules outside its solution version")

    adjacency: dict[UUID, list[SolutionDependency]] = defaultdict(list)
    for dependency in solution.dependencies:
        if (
            dependency.producer_module_id not in module_set
            or dependency.consumer_module_id not in module_set
        ):
            raise ValueError("solution dependency references modules outside its solution version")
        adjacency[dependency.producer_module_id].append(dependency)
    for edges in adjacency.values():
        edges.sort(
            key=lambda edge: (
                str(edge.consumer_module_id),
                edge.kind.value,
                edge.interface_key,
            )
        )

    # The order of seeds and neighbors is fixed, so a diamond graph chooses a
    # stable shortest trace without depending on task completion order.
    queue: deque[tuple[UUID, tuple[UUID, ...], tuple[str, ...], tuple[str, ...]]] = deque(
        (module_id, (module_id,), (), ()) for module_id in sorted(direct_set, key=str)
    )
    paths: dict[UUID, ImpactDependencyPath] = {
        module_id: ImpactDependencyPath(
            origin_module_id=module_id,
            affected_module_id=module_id,
            module_path=(module_id,),
            interface_keys=(),
            evidence_refs=(),
        )
        for module_id in direct_set
    }
    while queue:
        current, module_path, interface_keys, evidence_refs = queue.popleft()
        origin = module_path[0]
        for dependency in adjacency.get(current, ()):  # producer → consumer
            target = dependency.consumer_module_id
            if target in paths:
                continue
            next_path = (*module_path, target)
            next_interfaces = (*interface_keys, dependency.interface_key)
            next_evidence = tuple(dict.fromkeys((*evidence_refs, *dependency.evidence_refs)))
            paths[target] = ImpactDependencyPath(
                origin_module_id=origin,
                affected_module_id=target,
                module_path=next_path,
                interface_keys=next_interfaces,
                evidence_refs=next_evidence,
            )
            queue.append((target, next_path, next_interfaces, next_evidence))

    affected_set = set(paths)
    direct = tuple(module_id for module_id in module_ids if module_id in direct_set)
    transitive = tuple(
        module_id for module_id in module_ids if module_id in affected_set - direct_set
    )
    affected = tuple(module_id for module_id in module_ids if module_id in affected_set)
    unaffected = tuple(module_id for module_id in module_ids if module_id not in affected_set)
    return StructuralImpactPartition(
        change_set_id=change_set.change_set_id,
        direct_module_ids=direct,
        transitive_module_ids=transitive,
        affected_module_ids=affected,
        unaffected_module_ids=unaffected,
        paths=tuple(paths[module_id] for module_id in affected),
    )


def build_impact_change_plan(
    *,
    change_set: SolutionChangeSet,
    partition: StructuralImpactPartition,
) -> ImpactChangePlan:
    """Classify a bounded frontier without allowing an LLM to expand it.

    The rules are intentionally conservative.  They identify the minimum
    action that must happen because of the typed trigger and graph relation;
    a later semantic analyst can add a *candidate* ``RESEARCH``/``BLOCKED``
    conclusion only inside this frontier and only through Result Admission.
    """

    if partition.change_set_id != change_set.change_set_id:
        raise ValueError("impact partition does not belong to the change set")
    path_by_module = {path.affected_module_id: path for path in partition.paths}
    direct_set = set(partition.direct_module_ids)
    change_kinds = set(change_set.change_kinds)
    reason_codes = tuple(sorted(f"change.{item.value}" for item in change_kinds))
    semantic_required = bool(
        change_kinds
        & {
            # These triggers may carry implicit constraints outside the typed
            # dependency graph.  They do not enlarge the frontier by default.
            SolutionChangeKind.REQUIREMENT,
            SolutionChangeKind.MODULE,
            SolutionChangeKind.INTERFACE,
            SolutionChangeKind.OBSERVATION,
            SolutionChangeKind.VERIFICATION,
        }
    )

    def disposition_for(module_id: UUID) -> ImpactDisposition:
        if module_id not in direct_set:
            if SolutionChangeKind.REQUIREMENT in change_kinds:
                return ImpactDisposition.RECOMPUTE
            return ImpactDisposition.REVERIFY
        if SolutionChangeKind.CANDIDATE in change_kinds:
            return ImpactDisposition.REDECIDE
        if SolutionChangeKind.EVIDENCE in change_kinds:
            return ImpactDisposition.REVALIDATE
        if SolutionChangeKind.REQUIREMENT in change_kinds:
            return ImpactDisposition.RECOMPUTE
        return ImpactDisposition.REVERIFY

    def path_for(module_id: UUID) -> ImpactDependencyPath:
        """Return an explicit direct path for legacy projections without edges.

        Pre-projection solution versions retain a bounded structural partition
        but do not carry immutable dependency-path records.  A directly
        affected module is still a valid one-node frontier; failing with a
        ``KeyError`` would incorrectly make that historical data unusable.
        """
        return path_by_module.get(
            module_id,
            ImpactDependencyPath(
                origin_module_id=module_id,
                affected_module_id=module_id,
                module_path=(module_id,),
                interface_keys=(),
                evidence_refs=(),
            ),
        )

    classifications = tuple(
        ImpactClassification(
            module_id=module_id,
            disposition=disposition_for(module_id),
            reason_codes=reason_codes,
            dependency_path=path_for(module_id),
            semantic_analysis_required=semantic_required,
        )
        for module_id in partition.affected_module_ids
    )
    return ImpactChangePlan(
        change_set_id=change_set.change_set_id,
        base_solution_version_id=change_set.base_solution_version_id,
        classifications=classifications,
        unchanged_module_ids=partition.unaffected_module_ids,
        semantic_analysis_module_ids=(partition.affected_module_ids if semantic_required else ()),
    )


def build_steering_preview(plan: ImpactChangePlan) -> SteeringPreview:
    """Derive a compact, deterministic review preview from a Change Plan."""

    required_dispositions = tuple(
        sorted({item.disposition for item in plan.classifications}, key=str)
    )
    return SteeringPreview(
        change_set_id=plan.change_set_id,
        affected_module_ids=tuple(item.module_id for item in plan.classifications),
        unchanged_module_ids=plan.unchanged_module_ids,
        required_dispositions=required_dispositions,
        semantic_analysis_required=bool(plan.semantic_analysis_module_ids),
    )


__all__ = [
    "ImpactDependencyPath",
    "ImpactChangePlan",
    "ImpactClassification",
    "ImpactDisposition",
    "SteeringPreview",
    "StructuralImpactPartition",
    "build_impact_change_plan",
    "build_steering_preview",
    "traverse_solution_dependencies",
]
