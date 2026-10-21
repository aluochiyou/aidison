from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

from aidison.solution.contracts import SolutionContract, SolutionRiskClass
from aidison.solution.integration import IntegrationCheckReport, IntegrationCheckStatus
from aidison.solution.proposal_manifest import (
    SolutionProposalReadiness,
    build_solution_proposal_manifest,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _contract() -> SolutionContract:
    return SolutionContract(
        solution_run_id=uuid4(),
        project_id=uuid4(),
        basis_hash=_hash("basis"),
        basis_project_revision=2,
        requirement_refs=("requirement://revision/2",),
        module_revision_refs=("module://power/revision/2",),
        accepted_decision_refs=("decision://power/option/approved",),
        admitted_evidence_refs=("evidence-binding://power/1",),
        constraint_set_ref="artifact://constraints/2",
        interface_contract_refs=(),
        coverage_contract_ref="artifact://coverage/2",
        risk_class=SolutionRiskClass.STANDARD,
        allowed_tool_ids=(),
        budget_ref="budget://2",
        verification_policy_ref="policy://verification/standard",
        completion_policy_ref="policy://completion/2",
    )


def _manifest(*, outcome: IntegrationCheckStatus, unresolved: tuple[str, ...] = ()):
    return build_solution_proposal_manifest(
        contract=_contract(),
        run_contract_ref="artifact://solution-contract/2",
        normalized_elements_ref="artifact://elements/2",
        normalized_elements_hash=_hash("elements"),
        integration_report_ref="artifact://integration/2",
        integration_report_hash=_hash("integration"),
        integration=IntegrationCheckReport(findings=(), outcome=outcome),
        unresolved_refs=unresolved,
    )


def test_only_passed_integration_without_unknowns_can_create_solution_version() -> None:
    ready = _manifest(outcome=IntegrationCheckStatus.PASSED)
    partial = _manifest(outcome=IntegrationCheckStatus.PASSED, unresolved=("artifact://unknown/1",))
    pending = _manifest(outcome=IntegrationCheckStatus.NEEDS_VERIFICATION)
    blocked = _manifest(outcome=IntegrationCheckStatus.BLOCKED)

    assert ready.readiness is SolutionProposalReadiness.READY
    assert ready.can_create_solution_version is True
    assert partial.readiness is SolutionProposalReadiness.PARTIAL
    assert pending.readiness is SolutionProposalReadiness.NEEDS_VERIFICATION
    assert blocked.readiness is SolutionProposalReadiness.BLOCKED
    assert not partial.can_create_solution_version
    assert not pending.can_create_solution_version
    assert not blocked.can_create_solution_version
    assert ready.content_hash is not None
