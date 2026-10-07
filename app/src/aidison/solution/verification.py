"""Risk-triggered, read-only System Integration Verifier task planning."""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aidison.solution.contracts import SolutionContract, SolutionRiskClass
from aidison.solution.elements import (
    InterfaceContract,
    InterfaceVerificationStatus,
    SolutionElementSet,
)
from aidison.solution.integration import IntegrationCheckReport, IntegrationCheckStatus
from aidison.solution.verifier import InterfaceVerifierOutcome, InterfaceVerifierPayload


class SolutionVerifierPolicy(BaseModel):
    """Bound independent verification so it cannot become a mandatory agent layer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tasks: int = Field(default=8, ge=0, le=32)


class SolutionInterfaceVerifierTask(BaseModel):
    """One untrusted, read-only candidate check for an interface contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    interface_id: UUID
    task_key: str = Field(pattern=r"^solution\.verify\.[a-f0-9-]{36}$")
    input_refs: tuple[str, ...] = Field(max_length=128)
    allowed_tool_ids: tuple[str, ...] = Field(max_length=32)
    budget_ref: str = Field(min_length=1, max_length=500)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=8)
    idempotency_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_may_mutate_domain: bool = False


class SolutionVerifierPlan(BaseModel):
    """Deterministic task proposal; a future verifier execution remains separately admitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[SolutionInterfaceVerifierTask, ...]
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=8)


class AdmittedInterfaceVerifierObservation(BaseModel):
    """One already-admitted verifier observation available to consolidation.

    The ``result_ref`` is the immutable admitted result identity.  This type
    deliberately does not accept candidate (not-yet-admitted) verifier output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_ref: str = Field(min_length=1, max_length=500)
    payload: InterfaceVerifierPayload


class InterfaceVerificationProjection(BaseModel):
    """Deterministic derived interface status, never a canonical Domain write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    elements: SolutionElementSet
    admitted_result_refs: tuple[str, ...]
    conflicted_interface_ids: tuple[UUID, ...]


def route_solution_verifier_tasks(
    *,
    contract: SolutionContract,
    elements: SolutionElementSet,
    integration: IntegrationCheckReport,
    policy: SolutionVerifierPolicy | None = None,
) -> SolutionVerifierPlan:
    """Propose only risk-justified, independently executable interface checks.

    Standard runs retain the deterministic gate result without an extra model
    call. Elevated runs escalate interfaces explicitly marked unverified.
    High-risk runs additionally require an independent audit of interfaces that
    were already declared verified; blocked integration never dispatches a
    verifier because it cannot repair structural wiring.
    """

    policy = policy or SolutionVerifierPolicy()
    if integration.outcome is IntegrationCheckStatus.BLOCKED:
        return SolutionVerifierPlan(tasks=(), reason_codes=("integration_blocked",))
    if contract.risk_class is SolutionRiskClass.STANDARD:
        return SolutionVerifierPlan(tasks=(), reason_codes=("solution_verifier_not_required",))

    needs_verification = {
        item.interface_id
        for item in integration.findings
        if item.status is IntegrationCheckStatus.NEEDS_VERIFICATION
    }
    selected: list[tuple[InterfaceContract, tuple[str, ...]]] = []
    for interface in sorted(
        elements.interfaces, key=lambda item: (item.interface_key, str(item.id))
    ):
        if interface.id in needs_verification:
            selected.append((interface, ("interface_needs_verification",)))
        elif (
            contract.risk_class is SolutionRiskClass.HIGH
            and interface.verification_status is InterfaceVerificationStatus.VERIFIED
        ):
            selected.append((interface, ("high_risk_independent_interface_audit",)))

    if not selected:
        return SolutionVerifierPlan(tasks=(), reason_codes=("solution_verifier_not_required",))
    if len(selected) > policy.max_tasks:
        return SolutionVerifierPlan(tasks=(), reason_codes=("solution_verifier_task_limit",))
    tasks = tuple(
        _task(contract=contract, interface=interface, reason_codes=reason_codes)
        for interface, reason_codes in selected
    )
    return SolutionVerifierPlan(
        tasks=tasks,
        reason_codes=("risk_based_solution_verifier_tasks_created",),
    )


def apply_admitted_interface_verifier_observations(
    *,
    elements: SolutionElementSet,
    observations: tuple[AdmittedInterfaceVerifierObservation, ...],
) -> InterfaceVerificationProjection:
    """Derive interface state from admitted verifier observations only.

    A conflicting observation wins over a confirmation as a safety state, so
    changing completion order cannot silently turn a disagreement into truth.
    ``needs_more_evidence`` never upgrades or downgrades an interface by itself.
    """

    result_refs = tuple(item.result_ref for item in observations)
    if len(set(result_refs)) != len(result_refs):
        raise ValueError("admitted verifier result references must be unique")

    interfaces_by_id = {interface.id: interface for interface in elements.interfaces}
    outcomes_by_interface: dict[UUID, set[InterfaceVerifierOutcome]] = {}
    for observation in observations:
        interface_id = observation.payload.interface_id
        if interface_id not in interfaces_by_id:
            raise ValueError("admitted verifier observation references an unknown interface")
        outcomes_by_interface.setdefault(interface_id, set()).add(observation.payload.outcome)

    conflicted_interface_ids: list[UUID] = []
    projected_interfaces: list[InterfaceContract] = []
    for interface in elements.interfaces:
        outcomes = outcomes_by_interface.get(interface.id, set())
        if InterfaceVerifierOutcome.CONFLICTED in outcomes:
            projected_interfaces.append(
                interface.model_copy(
                    update={"verification_status": InterfaceVerificationStatus.CONFLICTED}
                )
            )
            conflicted_interface_ids.append(interface.id)
        elif InterfaceVerifierOutcome.CONFIRMED in outcomes:
            projected_interfaces.append(
                interface.model_copy(
                    update={"verification_status": InterfaceVerificationStatus.VERIFIED}
                )
            )
        else:
            projected_interfaces.append(interface)

    return InterfaceVerificationProjection(
        elements=SolutionElementSet(
            elements=elements.elements,
            interfaces=tuple(projected_interfaces),
        ),
        admitted_result_refs=tuple(sorted(result_refs)),
        conflicted_interface_ids=tuple(sorted(conflicted_interface_ids, key=str)),
    )


def _task(
    *,
    contract: SolutionContract,
    interface: InterfaceContract,
    reason_codes: tuple[str, ...],
) -> SolutionInterfaceVerifierTask:
    payload = {
        "run_id": str(contract.solution_run_id),
        "basis_hash": contract.basis_hash,
        "interface_id": str(interface.id),
        "input_refs": interface.evidence_refs,
        "allowed_tool_ids": contract.allowed_tool_ids,
        "budget_ref": contract.budget_ref,
        "reason_codes": reason_codes,
    }
    return SolutionInterfaceVerifierTask(
        run_id=contract.solution_run_id,
        project_id=contract.project_id,
        basis_hash=contract.basis_hash,
        interface_id=interface.id,
        task_key=f"solution.verify.{interface.id}",
        input_refs=interface.evidence_refs,
        allowed_tool_ids=contract.allowed_tool_ids,
        budget_ref=contract.budget_ref,
        reason_codes=reason_codes,
        idempotency_hash=sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


__all__ = [
    "AdmittedInterfaceVerifierObservation",
    "apply_admitted_interface_verifier_observations",
    "InterfaceVerificationProjection",
    "SolutionInterfaceVerifierTask",
    "SolutionVerifierPlan",
    "SolutionVerifierPolicy",
    "route_solution_verifier_tasks",
]
