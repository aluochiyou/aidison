from __future__ import annotations

from uuid import uuid4

from aidison.research.project_verification import (
    ProjectVerificationInput,
    VerificationCheckerKind,
    VerificationContract,
    VerificationCoverageObservation,
    VerificationRiskClass,
    VerificationRoute,
    VerificationVerifierPolicy,
    evaluate_project_verification,
    route_independent_verifier_tasks,
)


def _contract() -> VerificationContract:
    return VerificationContract(
        verification_id=uuid4(),
        project_id=uuid4(),
        subject_ref="solution-version://candidate/3",
        subject_revision=3,
        basis_hash="a" * 64,
        requirement_refs=("requirement://project/7",),
        constraint_refs=("constraint://budget/7",),
        coverage_keys=("fit", "safety"),
        risk_class=VerificationRiskClass.HIGH,
        required_checkers=(VerificationCheckerKind.DETERMINISTIC,),
        allowed_tool_ids=(),
        evidence_freshness_policy_ref="policy://freshness/v1",
        budget_ref="budget://verification/1",
        completion_policy_ref="policy://verification-completion/v1",
    )


def test_verification_routes_missing_coverage_and_stale_evidence_to_evidence_gap() -> None:
    contract = _contract()
    report = evaluate_project_verification(
        ProjectVerificationInput(
            contract=contract,
            coverage=(
                VerificationCoverageObservation(coverage_key="fit", satisfied=True),
                VerificationCoverageObservation(coverage_key="safety", satisfied=False),
            ),
            stale_evidence_refs=("evidence://safety/stale",),
        )
    )

    assert report.route is VerificationRoute.EVIDENCE_GAP
    assert {finding.finding_type.value for finding in report.findings} == {
        "missing_coverage",
        "stale_evidence",
    }
    assert report.verified is False


def test_verification_routes_constraint_failure_to_change_proposal_without_domain_mutation() -> (
    None
):
    contract = _contract()
    report = evaluate_project_verification(
        ProjectVerificationInput(
            contract=contract,
            coverage=tuple(
                VerificationCoverageObservation(coverage_key=key, satisfied=True)
                for key in contract.coverage_keys
            ),
            constraint_violation_refs=("constraint-result://budget-exceeded",),
        )
    )

    assert report.route is VerificationRoute.CHANGE_PROPOSAL
    assert report.findings[0].finding_type.value == "requirement_violation"
    assert report.subject_ref == contract.subject_ref
    assert report.subject_revision == contract.subject_revision


def test_verification_never_conflates_user_judgment_with_verified_fact() -> None:
    contract = _contract()
    report = evaluate_project_verification(
        ProjectVerificationInput(
            contract=contract,
            coverage=tuple(
                VerificationCoverageObservation(coverage_key=key, satisfied=True)
                for key in contract.coverage_keys
            ),
            needs_user_judgment_refs=("tradeoff://weight-vs-endurance",),
        )
    )

    assert report.route is VerificationRoute.DECISION_REQUEST
    assert report.verified is False
    assert report.findings[0].finding_type.value == "needs_user_judgment"


def test_independent_verifier_is_selective_and_only_proposes_private_tasks() -> None:
    contract = _contract()
    report = evaluate_project_verification(
        ProjectVerificationInput(
            contract=contract,
            coverage=tuple(
                VerificationCoverageObservation(coverage_key=key, satisfied=True)
                for key in contract.coverage_keys
            ),
            conflict_refs=("conflict://power-interface",),
        )
    )

    decision = route_independent_verifier_tasks(
        contract=contract,
        report=report,
        policy=VerificationVerifierPolicy(max_tasks=2),
    )

    assert len(decision.tasks) == 1
    assert decision.tasks[0].finding_ids == (report.findings[0].id,)
    assert decision.tasks[0].allowed_tool_ids == ()
    assert decision.tasks[0].result_may_mutate_domain is False


def test_independent_verifier_is_not_started_for_low_risk_missing_coverage() -> None:
    contract = _contract().model_copy(update={"risk_class": VerificationRiskClass.LOW})
    report = evaluate_project_verification(
        ProjectVerificationInput(
            contract=contract,
            coverage=(VerificationCoverageObservation(coverage_key="fit", satisfied=False),),
        )
    )

    decision = route_independent_verifier_tasks(contract=contract, report=report)

    assert decision.tasks == ()
    assert decision.reason_codes == ("independent_verifier_not_required",)
