from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.solution.elements import (
    InterfaceContract,
    InterfaceType,
    InterfaceVerificationStatus,
    SolutionElement,
    SolutionElementSet,
)


def _interface(
    *, producer: object | None = None, consumer: object | None = None
) -> InterfaceContract:
    return InterfaceContract(
        interface_key="power.supply-to-controller",
        producer_module_id=producer or uuid4(),
        consumer_module_id=consumer or uuid4(),
        interface_type=InterfaceType.ELECTRICAL,
        schema_or_unit="V",
        direction="source_to_load",
        range_or_capacity="11.1-12.6 V; 20 A continuous",
        environmental_conditions=("indoor",),
        version=1,
        evidence_refs=("evidence://battery/spec",),
        verification_status=InterfaceVerificationStatus.UNVERIFIED,
    )


def _element(*, module_id: object | None = None, **changes: object) -> SolutionElement:
    values: dict[str, object] = {
        "element_key": "power.battery-pack",
        "module_id": module_id or uuid4(),
        "responsibility": "Provide electrical power.",
        "selected_candidate_ref": "candidate://battery/1",
        "configuration_ref": "artifact://configuration/battery/1",
        "input_interface_ids": (),
        "output_interface_ids": (),
        "constraint_refs": ("artifact://constraints/safety",),
        "evidence_refs": ("evidence://battery/spec",),
        "decision_refs": ("decision://battery/approved",),
        "unresolved_refs": (),
    }
    values.update(changes)
    return SolutionElement(**values)


def test_solution_elements_and_interfaces_are_typed_proposal_boundaries() -> None:
    producer_id = uuid4()
    consumer_id = uuid4()
    interface = _interface(producer=producer_id, consumer=consumer_id)
    producer = _element(module_id=producer_id, output_interface_ids=(interface.id,))
    consumer = _element(module_id=consumer_id, input_interface_ids=(interface.id,))

    result = SolutionElementSet(elements=(producer, consumer), interfaces=(interface,))

    assert result.interfaces[0].verification_status is InterfaceVerificationStatus.UNVERIFIED
    assert "module_snapshot" not in SolutionElement.model_fields
    assert "project_revision" not in SolutionElement.model_fields


def test_solution_element_set_rejects_untyped_or_unclosed_interface_edges() -> None:
    module_id = uuid4()
    with pytest.raises(ValidationError, match="cannot connect a module to itself"):
        _interface(producer=module_id, consumer=module_id)

    element = _element(output_interface_ids=(uuid4(),))
    with pytest.raises(ValidationError, match="unknown InterfaceContract"):
        SolutionElementSet(elements=(element,), interfaces=())

    first = _element(module_id=module_id)
    second = _element(module_id=module_id, element_key="power.duplicate")
    with pytest.raises(ValidationError, match="one element per module"):
        SolutionElementSet(elements=(first, second), interfaces=())
