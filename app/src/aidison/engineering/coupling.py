"""Pure analysis for cyclic engineering couplings, separate from task DAGs."""

from __future__ import annotations

from collections import defaultdict, deque
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CouplingKind(StrEnum):
    MECHANICAL = "mechanical"
    POWER = "power"
    SIGNAL = "signal"
    PROTOCOL = "protocol"
    THERMAL = "thermal"
    MASS = "mass"
    COST = "cost"
    EVIDENCE = "evidence"


class CouplingCriticality(StrEnum):
    MUST = "must"
    SHOULD = "should"


class EngineeringCouplingEdge(BaseModel):
    """One directed interface/resource coupling between stable module lineages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_lineage_id: UUID
    target_lineage_id: UUID
    kind: CouplingKind
    criticality: CouplingCriticality = CouplingCriticality.SHOULD
    contract_ref: str | None = Field(default=None, max_length=500)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def cannot_be_self_referential(self) -> EngineeringCouplingEdge:
        if self.source_lineage_id == self.target_lineage_id:
            raise ValueError("engineering coupling edge cannot be self-referential")
        return self


class CouplingAnalysis(BaseModel):
    """Deterministic read model; it never creates or changes task dependencies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strongly_connected_components: tuple[tuple[UUID, ...], ...]
    condensation_edges: tuple[tuple[int, int], ...]
    transitive_reduction_edges: tuple[tuple[int, int], ...]


def impacted_component_ids(
    *, analysis: CouplingAnalysis, source_lineage_id: UUID
) -> tuple[int, ...]:
    """Return the source SCC and all downstream SCCs in stable order."""

    source_component = next(
        (
            index
            for index, component in enumerate(analysis.strongly_connected_components)
            if source_lineage_id in component
        ),
        None,
    )
    if source_component is None:
        raise ValueError("impact source lineage is absent from coupling analysis")
    adjacency: dict[int, set[int]] = defaultdict(set)
    for source, target in analysis.condensation_edges:
        adjacency[source].add(target)
    pending: deque[int] = deque([source_component])
    visited = {source_component}
    while pending:
        current = pending.popleft()
        for successor in sorted(adjacency[current]):
            if successor not in visited:
                visited.add(successor)
                pending.append(successor)
    return tuple(sorted(visited))


def analyze_couplings(
    *,
    lineage_ids: tuple[UUID, ...],
    edges: tuple[EngineeringCouplingEdge, ...],
) -> CouplingAnalysis:
    """Return SCC/condensation/reduction with canonical ordering.

    Multiple typed edges are intentionally allowed between the same pair. They
    collapse only for reachability analysis; their type/evidence remains on
    the original edge set. Unknown lineages fail closed rather than becoming
    implicit graph nodes.
    """

    nodes = set(lineage_ids)
    if len(nodes) != len(lineage_ids):
        raise ValueError("engineering coupling lineage IDs must be unique")
    if any(
        edge.source_lineage_id not in nodes or edge.target_lineage_id not in nodes
        for edge in edges
    ):
        raise ValueError("engineering coupling edge references an unknown lineage")
    adjacency: dict[UUID, set[UUID]] = defaultdict(set)
    for edge in edges:
        adjacency[edge.source_lineage_id].add(edge.target_lineage_id)

    index = 0
    indices: dict[UUID, int] = {}
    lowlinks: dict[UUID, int] = {}
    stack: list[UUID] = []
    on_stack: set[UUID] = set()
    components: list[tuple[UUID, ...]] = []

    def visit(node: UUID) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency[node], key=str):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: list[UUID] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(tuple(sorted(component, key=str)))

    for node in sorted(nodes, key=str):
        if node not in indices:
            visit(node)
    ordered_components = tuple(sorted(components, key=lambda item: tuple(map(str, item))))
    component_by_node = {
        node: component_index
        for component_index, component in enumerate(ordered_components)
        for node in component
    }
    condensed = {
        (component_by_node[edge.source_lineage_id], component_by_node[edge.target_lineage_id])
        for edge in edges
        if component_by_node[edge.source_lineage_id] != component_by_node[edge.target_lineage_id]
    }
    reduction = _transitive_reduction(node_count=len(ordered_components), edges=condensed)
    return CouplingAnalysis(
        strongly_connected_components=ordered_components,
        condensation_edges=tuple(sorted(condensed)),
        transitive_reduction_edges=tuple(sorted(reduction)),
    )


def _transitive_reduction(*, node_count: int, edges: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Reduce a DAG by testing alternate paths; condensation guarantees acyclicity."""

    reduced = set(edges)
    adjacency: dict[int, set[int]] = defaultdict(set)
    for source, target in edges:
        adjacency[source].add(target)
    for source, target in sorted(edges):
        adjacency[source].remove(target)
        queue: deque[int] = deque([source])
        visited = {source}
        while queue:
            current = queue.popleft()
            for successor in adjacency[current]:
                if successor not in visited:
                    visited.add(successor)
                    queue.append(successor)
        if target in visited:
            reduced.remove((source, target))
        adjacency[source].add(target)
    return reduced


__all__ = [
    "CouplingAnalysis",
    "CouplingCriticality",
    "CouplingKind",
    "EngineeringCouplingEdge",
    "analyze_couplings",
    "impacted_component_ids",
]
