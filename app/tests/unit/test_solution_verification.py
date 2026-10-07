from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from aidison.solution.contracts import SolutionContract, SolutionRiskClass
from aidison.solution.elements import (
    InterfaceContract,
    InterfaceQuantityConstraint,
    InterfaceType,
    InterfaceVerificationStatus,
    SolutionElement,
    SolutionElementSet,
)
from aidison.solution.integration import check_solution_integration
from aidison.solution.verification import (
    AdmittedInterfaceVerifierObservation,
    apply_admitted_interface_verifier_observations,
    route_solution_verifier_tasks,
)
from aidison.solution.verifier import InterfaceVerifierOutcome, InterfaceVerifierPayload


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _contract(*, risk_class: SolutionRiskClass) -> SolutionContract:
    return SolutionContract(
        solution_run_id=uuid4(),
        project_id=uuid4(),
        basis_hash=_hash("solution-verification"),
        basis_project_revision=2,
        requirement_refs=("requirement://fixture",),
        module_revision_refs=("module://producer", "module://consumer"),
        accepted_decision_refs=("decision://fixture/option/approved",),
        admitted_evidence_refs=("evidence-binding://fixture",),
        constraint_set_ref="artifact+sha256://constraints/fixture",
        interface_contract_refs=(),
        coverage_contract_ref="artifact+sha256://coverage/fixture",
        risk_class=risk_class,
        allowed_tool_ids=("web.read",),
        budget_ref="budget://fixture",
        verification_policy_ref=f"policy://solution-verification/{risk_class.value}",
        completion_policy_ref="policy://solution-completion/v1",
    )


def _elements(*, status: InterfaceVerificationStatus) -> SolutionElementSet:
    producer_id = uuid4()
    consumer_id = uuid4()
    interface = InterfaceContract(
        interface_key="power.source-to-load",
        producer_module_id=producer_id,
        consumer_module_id=consumer_id,
        interface_type=InterfaceType.ELECTRICAL,
        schema_or_unit="V",
        direction="source_to_load",
        range_or_capacity="11.1-12.6 V; 20 A continuous",
        quantity_constraints=(
            InterfaceQuantityConstraint(
                metric_key="voltage",
                producer_unit="V",
                consumer_unit="V",
                producer_minimum=Decimal("11.1"),
                producer_maximum=Decimal("12.6"),
                consumer_minimum=Decimal("11.1"),
                consumer_maximum=Decimal("12.6"),
            ),
        ),
        version=1,
        evidence_refs=("evidence-binding://fixture",),
        verification_status=status,
    )
    return SolutionElementSet(
        elements=(
            SolutionElement(
                element_key="power.source",
                module_id=producer_id,
                responsibility="Produce bounded electrical power.",
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
                responsibility="Consume bounded electrical power.",
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


def test_standard_risk_does_not_create_a_fixed_verifier_layer() -> None:
    elements = _elements(status=InterfaceVerificationStatus.UNVERIFIED)

    plan = route_solution_verifier_tasks(
        contract=_contract(risk_class=SolutionRiskClass.STANDARD),
        elements=elements,
        integration=check_solution_integration(elements),
    )

    assert plan.tasks == ()
    assert plan.reason_codes == ("solution_verifier_not_required",)


def test_elevated_risk_proposes_one_read_only_task_for_unverified_interface() -> None:
    elements = _elements(status=InterfaceVerificationStatus.UNVERIFIED)
    contract = _contract(risk_class=SolutionRiskClass.ELEVATED)

    plan = route_solution_verifier_tasks(
        contract=contract,
        elements=elements,
        integration=check_solution_integration(elements),
    )

    assert len(plan.tasks) == 1
    task = plan.tasks[0]
    assert task.interface_id == elements.interfaces[0].id
    assert task.allowed_tool_ids == ("web.read",)
    assert task.result_may_mutate_domain is False
    assert task.reason_codes == ("interface_needs_verification",)


def test_high_risk_audits_a_verified_interface_independently() -> None:
    elements = _elements(status=InterfaceVerificationStatus.VERIFIED)

    plan = route_solution_verifier_tasks(
        contract=_contract(risk_class=SolutionRiskClass.HIGH),
        elements=elements,
        integration=check_solution_integration(elements),
    )

    assert len(plan.tasks) == 1
    assert plan.tasks[0].reason_codes == ("high_risk_independent_interface_audit",)


def test_admitted_confirmation_derives_a_new_verified_element_projection() -> None:
    elements = _elements(status=InterfaceVerificationStatus.UNVERIFIED)
    interface = elements.interfaces[0]

    projection = apply_admitted_interface_verifier_observations(
        elements=elements,
        observations=(
            AdmittedInterfaceVerifierObservation(
                result_ref="admitted://agent-run-results/observation-1",
                payload=InterfaceVerifierPayload(
                    interface_id=interface.id,
                    outcome=InterfaceVerifierOutcome.CONFIRMED,
                    summary="The approved evidence covers this interface.",
                    evidence_refs=("evidence-binding://fixture",),
                ),
            ),
        ),
    )

    assert (
        projection.elements.interfaces[0].verification_status
        is InterfaceVerificationStatus.VERIFIED
    )
    assert projection.conflicted_interface_ids == ()


def test_conflicting_admitted_observations_do_not_use_completion_order_as_truth() -> None:
    elements = _elements(status=InterfaceVerificationStatus.UNVERIFIED)
    interface = elements.interfaces[0]
    confirmed = InterfaceVerifierPayload(
        interface_id=interface.id,
        outcome=InterfaceVerifierOutcome.CONFIRMED,
        summary="Supported.",
        evidence_refs=("evidence-binding://fixture",),
    )
    conflicted = InterfaceVerifierPayload(
        interface_id=interface.id,
        outcome=InterfaceVerifierOutcome.CONFLICTED,
        summary="Conflicting source condition.",
        evidence_refs=("evidence-binding://fixture",),
    )

    projection = apply_admitted_interface_verifier_observations(
        elements=elements,
        observations=(
            AdmittedInterfaceVerifierObservation(
                result_ref="admitted://agent-run-results/a",
                payload=confirmed,
            ),
            AdmittedInterfaceVerifierObservation(
                result_ref="admitted://agent-run-results/b",
                payload=conflicted,
            ),
        ),
    )

    assert (
        projection.elements.interfaces[0].verification_status
        is InterfaceVerificationStatus.CONFLICTED
    )
    assert projection.conflicted_interface_ids == (interface.id,)
