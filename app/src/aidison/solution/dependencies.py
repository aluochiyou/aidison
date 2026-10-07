"""Derive frozen, typed Solution dependencies from normalized interfaces."""

from __future__ import annotations

from aidison.domain.models import SolutionDependency, SolutionDependencyKind
from aidison.solution.elements import SolutionElementSet


def derive_solution_dependencies(elements: SolutionElementSet) -> tuple[SolutionDependency, ...]:
    """Create a stable dependency projection without consulting live module state."""

    return tuple(
        SolutionDependency(
            producer_module_id=interface.producer_module_id,
            consumer_module_id=interface.consumer_module_id,
            kind=SolutionDependencyKind(interface.interface_type.value),
            interface_key=interface.interface_key,
            evidence_refs=interface.evidence_refs,
        )
        for interface in sorted(
            elements.interfaces,
            key=lambda item: (
                item.interface_key,
                str(item.producer_module_id),
                str(item.consumer_module_id),
                str(item.id),
            ),
        )
    )


__all__ = ["derive_solution_dependencies"]
