from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from aidison.evaluation.contracts import (
    CaseResult,
    CaseStatus,
    EvaluationCase,
    EvaluationLayer,
    EvaluationMode,
    EvaluationReport,
    EvaluationSummary,
    FixtureKind,
    FrozenFixtureManifest,
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


def test_evaluation_case_carries_a_six_layer_and_frozen_fixture_identity() -> None:
    case = EvaluationCase(
        key="security-ssrf",
        title="SSRF is blocked",
        metric_id="red_action",
        layer=EvaluationLayer.ROBUSTNESS_SECURITY,
        fixture_kind=FixtureKind.FAULT,
        fixture_revision="r4.05",
    )
    assert case.layer is EvaluationLayer.ROBUSTNESS_SECURITY
    assert case.fixture_kind is FixtureKind.FAULT
    assert len(case.content_hash) == 64
    assert case.content_hash == case.content_hash


def test_frozen_fixture_manifest_is_order_stable_and_rejects_duplicate_case_keys() -> None:
    one = EvaluationCase(key="case-one", title="one", metric_id="m")
    two = EvaluationCase(key="case-two", title="two", metric_id="m")
    first = FrozenFixtureManifest.from_cases(
        fixture_set_key="aidison-r4",
        fixture_set_revision="r4.05",
        cases=(one, two),
    )
    second = FrozenFixtureManifest.from_cases(
        fixture_set_key="aidison-r4",
        fixture_set_revision="r4.05",
        cases=(two, one),
    )
    assert first.content_hash == second.content_hash
    assert first.case_keys == ("case-one", "case-two")
    with pytest.raises(ValidationError, match="duplicate"):
        FrozenFixtureManifest(
            fixture_set_key="aidison-r4",
            fixture_set_revision="r4.05",
            case_hashes=(("case-one", "a" * 64), ("case-one", "b" * 64)),
        )


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
