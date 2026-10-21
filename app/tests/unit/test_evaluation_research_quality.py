from __future__ import annotations

import asyncio

from aidison.evaluation.contracts import CaseStatus, EvaluationCase, FixtureKind, MetricStatus
from aidison.evaluation.fixtures import FIXTURE_CASES
from aidison.evaluation.metrics import check_research_quality_projection
from aidison.evaluation.runner import run_evaluation


def _golden_case() -> EvaluationCase:
    return next(case for case in FIXTURE_CASES if case.key == "research-quality-projection-deep")


def test_research_quality_projection_golden_exercises_real_consolidation_and_projection() -> None:
    case = _golden_case()

    metric = check_research_quality_projection(inputs=case.inputs)

    assert metric.status is MetricStatus.PASS
    assert metric.payload["must_coverage"] == {
        "total": 2,
        "answered": 2,
        "completion_rate": 1.0,
    }
    assert metric.payload["source_diversity"] == {
        "minimum_satisfied": True,
        "failing_coverage_keys": [],
        "scope": "source_id_and_url_origin_not_publisher_independence",
    }
    assert metric.payload["claim_rejection"] == {
        "rejected": 1,
        "admitted": 4,
        "rate": 0.2,
    }
    assert metric.payload["explanation"] == {
        "complete": True,
        "unexplained_coverage_keys": [],
    }
    assert metric.payload["deep_policy_budget_consistent"] is True

    report = asyncio.run(run_evaluation(case_keys=(case.key,)))
    assert report.summary.all_passed
    assert report.cases[0].status is CaseStatus.PASSED


def test_research_quality_projection_rejects_duplicate_source_identity_for_a_must_key() -> None:
    case = _golden_case()
    mutated = {
        **case.inputs,
        "observations": [
            {
                **item,
                "source_ids": ["source:shared"],
            }
            if item["coverage_key"] == "power.acceptance.01"
            else item
            for item in case.inputs["observations"]
        ],
    }

    metric = check_research_quality_projection(inputs=mutated)

    assert metric.status is MetricStatus.FAIL
    assert metric.payload["source_diversity"] == {
        "minimum_satisfied": False,
        "failing_coverage_keys": ["power.acceptance.01"],
        "scope": "source_id_and_url_origin_not_publisher_independence",
    }
    report = asyncio.run(
        run_evaluation(
            cases=(
                EvaluationCase(
                    key="research-quality-duplicate-source-id",
                    title="Duplicate source identities cannot satisfy a two-source MUST key",
                    metric_id="research_quality_projection",
                    fixture_kind=FixtureKind.MUTATION,
                    inputs=mutated,
                ),
            )
        )
    )
    assert report.cases[0].status is CaseStatus.FAILED


def test_research_quality_projection_rejects_two_pages_from_one_origin_for_deep_must() -> None:
    case = _golden_case()
    mutated = {
        **case.inputs,
        "observations": [
            {
                **item,
                "source_origins": ["web-origin:https://shared-origin.example"],
            }
            if item["coverage_key"] == "power.acceptance.01"
            else item
            for item in case.inputs["observations"]
        ],
    }

    metric = check_research_quality_projection(inputs=mutated)

    assert metric.status is MetricStatus.FAIL
    assert metric.payload["source_diversity"] == {
        "minimum_satisfied": False,
        "failing_coverage_keys": ["power.acceptance.01"],
        "scope": "source_id_and_url_origin_not_publisher_independence",
    }


def test_research_quality_projection_rejects_unexplained_rejected_claims() -> None:
    case = _golden_case()
    mutated = {
        **case.inputs,
        "evidence_diagnostics": {
            "power.acceptance.01": {
                "rejected_claim_count": 1,
                "rejected_reason_codes": [],
            }
        },
    }

    metric = check_research_quality_projection(inputs=mutated)

    assert metric.status is MetricStatus.FAIL
    assert metric.payload["explanation"] == {
        "complete": False,
        "unexplained_coverage_keys": ["power.acceptance.01"],
    }
    report = asyncio.run(
        run_evaluation(
            cases=(
                EvaluationCase(
                    key="research-quality-unexplained-rejection",
                    title="Rejected claims require an explanation",
                    metric_id="research_quality_projection",
                    fixture_kind=FixtureKind.FAULT,
                    inputs=mutated,
                ),
            )
        )
    )
    assert report.cases[0].status is CaseStatus.FAILED


def test_research_quality_projection_rejects_deep_policy_budget_drift() -> None:
    case = _golden_case()
    mutated = {
        **case.inputs,
        "execution_policy": {
            **case.inputs["execution_policy"],
            "collection": {
                **case.inputs["execution_policy"]["collection"],
                "max_documents_total": 4,
            },
        },
    }

    metric = check_research_quality_projection(inputs=mutated)

    assert metric.status is MetricStatus.FAIL
    assert metric.payload["deep_policy_budget_consistent"] is False
