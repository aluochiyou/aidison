"""Deterministic cross-module checks for normalized Solution Elements.

This checker intentionally judges only structural integration facts that are
already explicit in typed proposal data.  It does not infer physical
compatibility from prose or use an LLM as a fan-in fact judge.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aidison.solution.elements import (
    InterfaceContract,
    InterfaceType,
    InterfaceVerificationStatus,
    SolutionElement,
    SolutionElementSet,
)

_QUANTITATIVE_INTERFACE_TYPES = frozenset(
    {
        InterfaceType.ELECTRICAL,
        InterfaceType.MECHANICAL,
        InterfaceType.THERMAL,
        InterfaceType.TIMING,
    }
)


class IntegrationCheckStatus(StrEnum):
    PASSED = "passed"
    NEEDS_VERIFICATION = "needs_verification"
    BLOCKED = "blocked"


class IntegrationCheckFinding(BaseModel):
    """One stable, inspectable result from a deterministic integration rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    interface_id: UUID
    status: IntegrationCheckStatus
    message: str = Field(min_length=1, max_length=2_000)


class IntegrationCheckReport(BaseModel):
    """Complete deterministic result; later gates decide whether to run a verifier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[IntegrationCheckFinding, ...]
    outcome: IntegrationCheckStatus


def _finding(
    *,
    rule_id: str,
    interface: InterfaceContract,
    status: IntegrationCheckStatus,
    message: str,
) -> IntegrationCheckFinding:
    return IntegrationCheckFinding(
        rule_id=rule_id,
        interface_id=interface.id,
        status=status,
        message=message,
    )


def _check_interface(
    *,
    interface: InterfaceContract,
    elements: dict[UUID, SolutionElement],
) -> tuple[IntegrationCheckFinding, ...]:
    """Evaluate explicit endpoint wiring and verifier prerequisites in stable order."""

    producer = elements[interface.producer_module_id]
    consumer = elements[interface.consumer_module_id]
    findings: list[IntegrationCheckFinding] = []

    if interface.id not in producer.output_interface_ids:
        findings.append(
            _finding(
                rule_id="integration.producer_output_missing",
                interface=interface,
                status=IntegrationCheckStatus.BLOCKED,
                message="producer element does not declare the interface as an output",
            )
        )
    if interface.id not in consumer.input_interface_ids:
        findings.append(
            _finding(
                rule_id="integration.consumer_input_missing",
                interface=interface,
                status=IntegrationCheckStatus.BLOCKED,
                message="consumer element does not declare the interface as an input",
            )
        )
    if interface.verification_status is InterfaceVerificationStatus.CONFLICTED:
        findings.append(
            _finding(
                rule_id="integration.interface_conflicted",
                interface=interface,
                status=IntegrationCheckStatus.BLOCKED,
                message="interface has a declared unresolved conflict",
            )
        )
    elif interface.verification_status is InterfaceVerificationStatus.BLOCKED:
        findings.append(
            _finding(
                rule_id="integration.interface_blocked",
                interface=interface,
                status=IntegrationCheckStatus.BLOCKED,
                message="interface is blocked before deterministic integration can pass",
            )
        )
    elif interface.verification_status is InterfaceVerificationStatus.UNVERIFIED:
        findings.append(
            _finding(
                rule_id="integration.interface_needs_verification",
                interface=interface,
                status=IntegrationCheckStatus.NEEDS_VERIFICATION,
                message="interface has not completed its required verification",
            )
        )
    elif not interface.evidence_refs:
        findings.append(
            _finding(
                rule_id="integration.verified_interface_without_evidence",
                interface=interface,
                status=IntegrationCheckStatus.BLOCKED,
                message="verified interface must retain at least one evidence reference",
            )
        )

    endpoint_evidence_refs = set(producer.evidence_refs) | set(consumer.evidence_refs)
    if not set(interface.evidence_refs) <= endpoint_evidence_refs:
        findings.append(
            _finding(
                rule_id="integration.interface_evidence_untraceable",
                interface=interface,
                status=IntegrationCheckStatus.BLOCKED,
                message=(
                    "interface evidence must be referenced by at least one endpoint element "
                    "to preserve proposal traceability"
                ),
            )
        )

    if interface.interface_type in _QUANTITATIVE_INTERFACE_TYPES:
        if not interface.quantity_constraints:
            findings.append(
                _finding(
                    rule_id="integration.interface_quantity_missing",
                    interface=interface,
                    status=IntegrationCheckStatus.NEEDS_VERIFICATION,
                    message=(
                        "physical or timing interface requires a structured quantity constraint; "
                        "free-text range_or_capacity is not machine-checkable"
                    ),
                )
            )
        for constraint in interface.quantity_constraints:
            if constraint.producer_unit != constraint.consumer_unit:
                findings.append(
                    _finding(
                        rule_id="integration.interface_quantity_unit_mismatch",
                        interface=interface,
                        status=IntegrationCheckStatus.BLOCKED,
                        message=(
                            f"quantity metric {constraint.metric_key} uses incompatible units "
                            f"{constraint.producer_unit} and {constraint.consumer_unit}"
                        ),
                    )
                )
            elif (
                constraint.producer_minimum > constraint.consumer_minimum
                or constraint.producer_maximum < constraint.consumer_maximum
            ):
                findings.append(
                    _finding(
                        rule_id="integration.interface_quantity_supply_insufficient",
                        interface=interface,
                        status=IntegrationCheckStatus.BLOCKED,
                        message=(
                            f"quantity metric {constraint.metric_key} does not cover "
                            "the consumer-required interval"
                        ),
                    )
                )

    if not findings:
        findings.append(
            _finding(
                rule_id="integration.interface_connected",
                interface=interface,
                status=IntegrationCheckStatus.PASSED,
                message="producer output, consumer input, and verified evidence are present",
            )
        )
    return tuple(findings)


def check_solution_integration(elements: SolutionElementSet) -> IntegrationCheckReport:
    """Run deterministic checks with output independent of input ordering."""

    by_module = {item.module_id: item for item in elements.elements}
    findings = tuple(
        finding
        for interface in sorted(
            elements.interfaces, key=lambda item: (item.interface_key, str(item.id))
        )
        for finding in _check_interface(interface=interface, elements=by_module)
    )
    if any(item.status is IntegrationCheckStatus.BLOCKED for item in findings):
        outcome = IntegrationCheckStatus.BLOCKED
    elif any(item.status is IntegrationCheckStatus.NEEDS_VERIFICATION for item in findings):
        outcome = IntegrationCheckStatus.NEEDS_VERIFICATION
    else:
        outcome = IntegrationCheckStatus.PASSED
    return IntegrationCheckReport(findings=findings, outcome=outcome)


__all__ = [
    "IntegrationCheckFinding",
    "IntegrationCheckReport",
    "IntegrationCheckStatus",
    "check_solution_integration",
]
