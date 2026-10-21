from uuid import UUID

import pytest

from aidison.engineering.coupling import (
    EngineeringCouplingEdge,
    analyze_couplings,
    impacted_component_ids,
)


def _id(value: int) -> UUID:
    return UUID(int=value)


def test_coupling_cycles_are_condensed_without_becoming_task_dag_edges() -> None:
    analysis = analyze_couplings(
        lineage_ids=(_id(1), _id(2), _id(3)),
        edges=(
            EngineeringCouplingEdge(
                source_lineage_id=_id(1), target_lineage_id=_id(2), kind="power"
            ),
            EngineeringCouplingEdge(
                source_lineage_id=_id(2), target_lineage_id=_id(1), kind="signal"
            ),
            EngineeringCouplingEdge(
                source_lineage_id=_id(2), target_lineage_id=_id(3), kind="protocol"
            ),
        ),
    )

    assert analysis.strongly_connected_components == ((_id(1), _id(2)), (_id(3),))
    assert analysis.condensation_edges == ((0, 1),)
    assert analysis.transitive_reduction_edges == ((0, 1),)


def test_transitive_reduction_removes_only_redundant_condensed_edge() -> None:
    analysis = analyze_couplings(
        lineage_ids=(_id(1), _id(2), _id(3)),
        edges=(
            EngineeringCouplingEdge(
                source_lineage_id=_id(1), target_lineage_id=_id(2), kind="power"
            ),
            EngineeringCouplingEdge(
                source_lineage_id=_id(2), target_lineage_id=_id(3), kind="signal"
            ),
            EngineeringCouplingEdge(
                source_lineage_id=_id(1), target_lineage_id=_id(3), kind="cost"
            ),
        ),
    )

    assert analysis.transitive_reduction_edges == ((0, 1), (1, 2))


def test_unknown_lineage_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown lineage"):
        analyze_couplings(
            lineage_ids=(_id(1),),
            edges=(
                EngineeringCouplingEdge(
                    source_lineage_id=_id(1), target_lineage_id=_id(2), kind="power"
                ),
            ),
        )


def test_impact_projection_returns_condensed_downstream_components() -> None:
    analysis = analyze_couplings(
        lineage_ids=(_id(1), _id(2), _id(3)),
        edges=(
            EngineeringCouplingEdge(
                source_lineage_id=_id(1), target_lineage_id=_id(2), kind="power"
            ),
            EngineeringCouplingEdge(
                source_lineage_id=_id(2), target_lineage_id=_id(3), kind="signal"
            ),
        ),
    )

    assert impacted_component_ids(analysis=analysis, source_lineage_id=_id(1)) == (0, 1, 2)
    assert impacted_component_ids(analysis=analysis, source_lineage_id=_id(3)) == (2,)
