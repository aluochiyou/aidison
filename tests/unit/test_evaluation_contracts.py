from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from aidison.evaluation.contracts import (
    CaseResult,
    CaseStatus,
    EvaluationCase,
    EvaluationMode,
    EvaluationReport,
    EvaluationSummary,
    MetricResult,
    MetricStatus,
    canonical_hash,
    derive_run_id,
    utc_now,
)


def _metric(status: MetricStatus = MetricStatus.PASS) -> MetricResult:
    return MetricResult(metric_id="evidence_completeness", status=status, detail="ok")


def test_metric_result_passed_property() -> None:
    assert _metric(MetricStatus.PASS).passed is True
    assert _metric(MetricStatus.FAIL).passed is False
    assert _metric(MetricStatus.ERROR).passed is False
    assert _metric(MetricStatus.SKIP).passed is False


def test_evaluation_case_key_must_match_slug_pattern() -> None:
    with pytest.raises(ValidationError):
        EvaluationCase(key="Not A Slug!", title="t", metric_id="m")
    case = EvaluationCase(key="evidence-refs-resolved", title="t", metric_id="m")
    assert case.key == "evidence-refs-resolved"


def _summary(
    *,
    case_count: int,
    passed: int,
    failed: int,
    errored: int = 0,
    skipped: int = 0,
) -> EvaluationSummary:
    return EvaluationSummary(
        case_count=case_count,
        passed=passed,
        failed=failed,
        errored=errored,
        skipped=skipped,
        duration_seconds=0.0,
    )


def test_summary_all_passed_requires_cases_and_no_failures() -> None:
    assert _summary(case_count=1, passed=1, failed=0).all_passed
    assert not _summary(case_count=1, passed=0, failed=1).all_passed
    assert not _summary(case_count=0, passed=0, failed=0).all_passed


def test_errored_case_result_requires_error_message() -> None:
    with pytest.raises(ValidationError, match="error message"):
        CaseResult(
            case_key="k",
            title="t",
            metric_id="m",
            status=CaseStatus.ERRORED,
            metrics=(_metric(MetricStatus.ERROR),),
            started_at=utc_now(),
            completed_at=utc_now(),
            duration_seconds=0.0,
        )


def test_report_summary_must_match_case_count() -> None:
    summary = _summary(case_count=1, passed=1, failed=0)
    result = CaseResult(
        case_key="k",
        title="t",
        metric_id="m",
        status=CaseStatus.PASSED,
        metrics=(_metric(),),
        started_at=utc_now(),
        completed_at=utc_now(),
        duration_seconds=0.0,
    )
    with pytest.raises(ValidationError, match="case_count"):
        EvaluationReport(
            run_id=UUID(int=1),
            mode=EvaluationMode.OFFLINE,
            generated_at=utc_now(),
            summary=summary,
            cases=(result, result),
        )


def test_derive_run_id_is_deterministic_and_order_stable() -> None:
    keys = ("b", "a")
    first = derive_run_id(mode=EvaluationMode.OFFLINE, case_keys=keys)
    second = derive_run_id(mode=EvaluationMode.OFFLINE, case_keys=("a", "b"))
    other_mode = derive_run_id(mode=EvaluationMode.LIVE, case_keys=keys)
    assert first == second
    assert first != other_mode
    assert isinstance(first, UUID)


def test_canonical_hash_is_order_and_shape_stable() -> None:
    assert canonical_hash("a", 1, "b") == canonical_hash("a", 1, "b")
    assert canonical_hash("a", 1, "b") != canonical_hash("a", 1, "c")
