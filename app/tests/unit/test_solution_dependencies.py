from __future__ import annotations

from uuid import uuid4

from aidison.domain.models import SolutionDependencyKind
from aidison.solution.dependencies import derive_solution_dependencies
from aidison.solution.elements import (
    InterfaceContract,
    InterfaceType,
    InterfaceVerificationStatus,
    SolutionElement,
    SolutionElementSet,
)


def test_solution_dependencies_are_stably_derived_from_frozen_interfaces() -> None:
    producer_id = uuid4()
    consumer_id = uuid4()
    interface = InterfaceContract(
        interface_key="power.feed",
        producer_module_id=producer_id,
        consumer_module_id=consumer_id,
        interface_type=InterfaceType.ELECTRICAL,
        schema_or_unit="V",
        direction="source_to_load",
        range_or_capacity="11.1-12.6 V",
        version=1,
        evidence_refs=("evidence-binding://fixture",),
        verification_status=InterfaceVerificationStatus.VERIFIED,
    )
    elements = SolutionElementSet(
        elements=(
            SolutionElement(
                element_key="power.source",
                module_id=producer_id,
                responsibility="Produce bounded power.",
                selected_candidate_ref="candidate://producer",
                configuration_ref="artifact://producer",
                input_interface_ids=(),
                output_interface_ids=(interface.id,),
                constraint_refs=(),
                evidence_refs=("evidence-binding://fixture",),
                decision_refs=("decision://fixture",),
                unresolved_refs=(),
            ),
            SolutionElement(
                element_key="power.load",
                module_id=consumer_id,
                responsibility="Consume bounded power.",
                selected_candidate_ref="candidate://consumer",
                configuration_ref="artifact://consumer",
                input_interface_ids=(interface.id,),
                output_interface_ids=(),
                constraint_refs=(),
                evidence_refs=("evidence-binding://fixture",),
                decision_refs=("decision://fixture",),
                unresolved_refs=(),
            ),
        ),
        interfaces=(interface,),
    )

    dependencies = derive_solution_dependencies(elements)

    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert dependency.producer_module_id == producer_id
    assert dependency.consumer_module_id == consumer_id
    assert dependency.kind is SolutionDependencyKind.ELECTRICAL
    assert dependency.interface_key == "power.feed"
    assert dependency.evidence_refs == ("evidence-binding://fixture",)
