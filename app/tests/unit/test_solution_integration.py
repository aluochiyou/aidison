from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from aidison.solution.elements import (
    InterfaceContract,
    InterfaceQuantityConstraint,
    InterfaceType,
    InterfaceVerificationStatus,
    SolutionElement,
    SolutionElementSet,
)
from aidison.solution.integration import (
    IntegrationCheckStatus,
    check_solution_integration,
)


def _quantity_constraint(
    *, producer_unit: str = "V", consumer_unit: str = "V", producer_maximum: str = "12.6"
) -> InterfaceQuantityConstraint:
    return InterfaceQuantityConstraint(
        metric_key="voltage",
        producer_unit=producer_unit,
        consumer_unit=consumer_unit,
        producer_minimum=Decimal("11.1"),
        producer_maximum=Decimal(producer_maximum),
        consumer_minimum=Decimal("11.1"),
        consumer_maximum=Decimal("12.6"),
    )


def _elements(
    *,
    producer_outputs: bool,
    consumer_inputs: bool,
    status: InterfaceVerificationStatus,
    quantity_constraints: tuple[InterfaceQuantityConstraint, ...] | None = None,
    interface_evidence_refs: tuple[str, ...] = ("evidence://battery/spec",),
    endpoint_evidence_refs: tuple[str, ...] = ("evidence://battery/spec",),
):
    producer_id = uuid4()
    consumer_id = uuid4()
    interface = InterfaceContract(
        interface_key="power.supply-to-controller",
        producer_module_id=producer_id,
        consumer_module_id=consumer_id,
        interface_type=InterfaceType.ELECTRICAL,
        schema_or_unit="V",
        direction="source_to_load",
        range_or_capacity="11.1-12.6 V; 20 A continuous",
        quantity_constraints=(
            (_quantity_constraint(),) if quantity_constraints is None else quantity_constraints
        ),
        version=1,
        evidence_refs=interface_evidence_refs,
        verification_status=status,
    )

    def element(
        module_id: object, key: str, *, inputs: tuple = (), outputs: tuple = ()
    ) -> SolutionElement:
        return SolutionElement(
            element_key=key,
            module_id=module_id,
            responsibility="Bounded system responsibility.",
            selected_candidate_ref=f"candidate://{key}/1",
            configuration_ref=f"artifact://configuration/{key}/1",
            input_interface_ids=inputs,
            output_interface_ids=outputs,
            constraint_refs=(),
            evidence_refs=endpoint_evidence_refs,
            decision_refs=("decision://battery/approved",),
            unresolved_refs=(),
        )

    return SolutionElementSet(
        elements=(
            element(
                producer_id, "power.source", outputs=(interface.id,) if producer_outputs else ()
            ),
            element(
                consumer_id, "power.consumer", inputs=(interface.id,) if consumer_inputs else ()
            ),
        ),
        interfaces=(interface,),
    )


def test_verified_complete_interface_passes_deterministic_integration() -> None:
    report = check_solution_integration(
        _elements(
            producer_outputs=True,
            consumer_inputs=True,
            status=InterfaceVerificationStatus.VERIFIED,
        )
    )

    assert report.outcome is IntegrationCheckStatus.PASSED
    assert report.findings[0].rule_id == "integration.interface_connected"


def test_unverified_interface_routes_to_verification_without_model_call() -> None:
    report = check_solution_integration(
        _elements(
            producer_outputs=True,
            consumer_inputs=True,
            status=InterfaceVerificationStatus.UNVERIFIED,
        )
    )

    assert report.outcome is IntegrationCheckStatus.NEEDS_VERIFICATION
    assert report.findings[0].rule_id == "integration.interface_needs_verification"


def test_missing_endpoint_or_declared_conflict_blocks_integration() -> None:
    report = check_solution_integration(
        _elements(
            producer_outputs=False,
            consumer_inputs=False,
            status=InterfaceVerificationStatus.CONFLICTED,
        )
    )

    assert report.outcome is IntegrationCheckStatus.BLOCKED
    assert {item.rule_id for item in report.findings} == {
        "integration.producer_output_missing",
        "integration.consumer_input_missing",
        "integration.interface_conflicted",
    }


def test_quantitative_interface_requires_exact_units_and_supply_coverage() -> None:
    unit_mismatch = check_solution_integration(
        _elements(
            producer_outputs=True,
            consumer_inputs=True,
            status=InterfaceVerificationStatus.VERIFIED,
            quantity_constraints=(_quantity_constraint(consumer_unit="mV"),),
        )
    )
    insufficient_supply = check_solution_integration(
        _elements(
            producer_outputs=True,
            consumer_inputs=True,
            status=InterfaceVerificationStatus.VERIFIED,
            quantity_constraints=(_quantity_constraint(producer_maximum="12.0"),),
        )
    )

    assert unit_mismatch.outcome is IntegrationCheckStatus.BLOCKED
    assert {item.rule_id for item in unit_mismatch.findings} == {
        "integration.interface_quantity_unit_mismatch"
    }
    assert insufficient_supply.outcome is IntegrationCheckStatus.BLOCKED
    assert {item.rule_id for item in insufficient_supply.findings} == {
        "integration.interface_quantity_supply_insufficient"
    }


def test_physical_interface_without_structured_quantities_remains_unverified() -> None:
    report = check_solution_integration(
        _elements(
            producer_outputs=True,
            consumer_inputs=True,
            status=InterfaceVerificationStatus.VERIFIED,
            quantity_constraints=(),
        )
    )

    assert report.outcome is IntegrationCheckStatus.NEEDS_VERIFICATION
    assert report.findings[0].rule_id == "integration.interface_quantity_missing"


def test_interface_evidence_must_be_traceable_to_an_endpoint_element() -> None:
    report = check_solution_integration(
        _elements(
            producer_outputs=True,
            consumer_inputs=True,
            status=InterfaceVerificationStatus.VERIFIED,
            interface_evidence_refs=("evidence://interface/spec",),
            endpoint_evidence_refs=("evidence://endpoint/spec",),
        )
    )

    assert report.outcome is IntegrationCheckStatus.BLOCKED
    assert report.findings[0].rule_id == "integration.interface_evidence_untraceable"
