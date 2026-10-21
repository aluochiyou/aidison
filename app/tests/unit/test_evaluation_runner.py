from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.evaluation.contracts import (
    CaseStatus,
    EvaluationCase,
    EvaluationLayer,
    EvaluationMode,
    EvaluationReport,
)
from aidison.evaluation.fixtures import FIXTURE_CASES, FROZEN_FIXTURE_MANIFEST
from aidison.evaluation.runner import (
    EvaluationNoCasesError,
    evaluate_case,
    run_evaluation,
)


def test_fixture_suite_passes_end_to_end() -> None:
    report = asyncio.run(run_evaluation())
    assert report.summary.passed == len(FIXTURE_CASES)
    assert report.summary.all_passed
    assert report.mode is EvaluationMode.OFFLINE
    assert report.reporter == "none"
    assert all(case.status is CaseStatus.PASSED for case in report.cases)
    assert report.fixture_manifest.content_hash == FROZEN_FIXTURE_MANIFEST.content_hash
    assert set(report.layers_covered) == set(EvaluationLayer)


def test_frozen_fixture_suite_covers_each_evaluation_layer_exactly_through_declared_cases() -> None:
    assert set(case.layer for case in FIXTURE_CASES) == set(EvaluationLayer)
    assert FROZEN_FIXTURE_MANIFEST.case_keys == tuple(sorted(case.key for case in FIXTURE_CASES))


def test_report_is_json_safe_and_does_not_contain_secrets() -> None:
    report = asyncio.run(run_evaluation())
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    assert isinstance(payload, str)
    for forbidden in ("api_key", "LANGSMITH_API_KEY", "sk-"):
        assert forbidden not in payload.lower()


def test_report_run_id_is_deterministic_across_runs() -> None:
    first = asyncio.run(run_evaluation())
    second = asyncio.run(run_evaluation())
    assert first.run_id == second.run_id
    assert first.cases[0].case_key == second.cases[0].case_key


def test_report_identity_changes_when_frozen_case_content_changes() -> None:
    base = EvaluationCase(
        key="fixture-identity",
        title="t",
        metric_id="structured_contract",
        inputs={"payload": {}, "schema": {"fields": {}}},
    )
    changed = base.model_copy(update={"description": "different frozen input contract"})
    first = asyncio.run(run_evaluation(cases=(base,)))
    second = asyncio.run(run_evaluation(cases=(changed,)))
    assert first.run_id != second.run_id
    assert first.fixture_manifest.content_hash != second.fixture_manifest.content_hash


def test_selected_case_subset_runs_only_requested_keys() -> None:
    report = asyncio.run(run_evaluation(case_keys=("structured-contract-complete",)))
    assert report.summary.case_count == 1
    assert report.cases[0].case_key == "structured-contract-complete"


def test_unknown_case_key_raises_evaluation_no_cases() -> None:
    with pytest.raises(EvaluationNoCasesError, match="unknown case keys"):
        asyncio.run(run_evaluation(case_keys=("does-not-exist",)))


def test_unknown_metric_id_produces_errored_case() -> None:
    case = EvaluationCase(key="broken-metric", title="t", metric_id="no_such_metric", inputs={})
    result = asyncio.run(evaluate_case(case))
    assert result.status is CaseStatus.ERRORED
    assert result.error is not None
    assert "unknown metric id" in result.error


def test_failed_fixture_produces_failed_case_and_summary() -> None:
    case = EvaluationCase(
        key="dangling-evidence",
        title="t",
        metric_id="evidence_completeness",
        inputs={
            "cited_refs": ["ref/ghost"],
            "available_refs": [],
            "min_evidence": 1,
        },
    )
    report = asyncio.run(run_evaluation(cases=(case,)))
    assert report.summary.failed == 1
    assert report.summary.all_passed is False
    assert report.cases[0].status is CaseStatus.FAILED


def test_offline_mode_never_contacts_a_reporter() -> None:
    class ExplodingReporter:
        name = "explode"

        @property
        def enabled(self) -> bool:
            return True

        def report(self, report: EvaluationReport) -> bool:
            raise AssertionError("reporter must not be called in offline mode")

    report = asyncio.run(
        run_evaluation(case_keys=("red-action-blocked",), reporter=ExplodingReporter())
    )
    assert report.reporter == "none"
    assert report.summary.all_passed


def test_live_mode_calls_enabled_reporter_and_marks_report() -> None:
    calls: list[EvaluationReport] = []

    class FakeReporter:
        name = "langsmith"

        @property
        def enabled(self) -> bool:
            return True

        def report(self, report: EvaluationReport) -> bool:
            calls.append(report)
            return True

    report = asyncio.run(
        run_evaluation(
            case_keys=("red-action-blocked",),
            mode=EvaluationMode.LIVE,
            reporter=FakeReporter(),
        )
    )
    assert len(calls) == 1
    assert report.reporter == "langsmith"


def test_live_mode_keeps_reporter_unset_when_delivery_fails() -> None:
    class UnavailableReporter:
        name = "langfuse"

        @property
        def enabled(self) -> bool:
            return True

        def report(self, report: EvaluationReport) -> bool:
            del report
            return False

    report = asyncio.run(
        run_evaluation(
            case_keys=("red-action-blocked",),
            mode=EvaluationMode.LIVE,
            reporter=UnavailableReporter(),
        )
    )

    assert report.summary.all_passed
    assert report.reporter == "none"


def test_live_mode_survives_a_nonconforming_reporter() -> None:
    class ExplodingReporter:
        name = "custom"

        @property
        def enabled(self) -> bool:
            return True

        def report(self, report: EvaluationReport) -> bool:
            del report
            raise ConnectionError("simulated exporter outage")

    report = asyncio.run(
        run_evaluation(
            case_keys=("red-action-blocked",),
            mode=EvaluationMode.LIVE,
            reporter=ExplodingReporter(),
        )
    )

    assert report.summary.all_passed
    assert report.reporter == "none"


def _result_input() -> dict[str, object]:
    basis = sha256(b"basis").hexdigest()
    result_id = str(uuid4())
    run_id = str(uuid4())
    return {
        "result": {
            "id": result_id,
            "run_id": run_id,
            "task_id": str(uuid4()),
            "basis_hash": basis,
            "producer_attempt_id": str(uuid4()),
            "producer_generation": 1,
            "producer_profile_ref": "profile://research/1",
            "status": "succeeded",
            "artifact_ref": "artifact+sha256://result/one",
            "manifest_hash": sha256(b"manifest").hexdigest(),
            "evidence_refs": ["artifact+sha256://evidence/one"],
            "coverage_observation_refs": [],
            "unresolved_refs": [],
        },
        "admission": {
            "run_id": run_id,
            "result_id": result_id,
            "result_manifest_hash": sha256(b"manifest").hexdigest(),
            "disposition": "accepted",
            "reason_codes": ["runtime_fenced"],
            "admitted_ref": "admitted://result/one",
        },
    }


def test_runner_builds_result_admission_case_from_json_safe_inputs() -> None:
    case = EvaluationCase(
        key="runtime-admission",
        title="t",
        metric_id="result_admission",
        inputs=_result_input(),
    )
    report = asyncio.run(run_evaluation(cases=(case,)))
    assert report.summary.passed == 1
    assert report.cases[0].status is CaseStatus.PASSED
