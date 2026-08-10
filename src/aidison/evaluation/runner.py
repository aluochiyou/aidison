"""Deterministic offline evaluation runner (application layer).

The runner composes fixed fixture cases with pure metric checkers and returns a
JSON-safe ``EvaluationReport``.  It performs no external I/O itself; an optional
reporter is only consulted in ``LIVE`` mode and can never raise into the report.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aidison.evaluation.contracts import (
    CaseResult,
    CaseStatus,
    EvaluationCase,
    EvaluationMode,
    EvaluationReport,
    EvaluationSummary,
    MetricResult,
    MetricStatus,
    derive_run_id,
    utc_now,
)
from aidison.evaluation.fixtures import FIXTURE_CASES
from aidison.evaluation.langsmith_reporter import EvaluationReporter
from aidison.evaluation.metrics import (
    StructuredSchemaSpec,
    check_evidence_completeness,
    check_red_action_blocked,
    check_replay_determinism,
    check_result_admission,
    check_structured_contract,
)
from aidison.runtime.contracts import (
    InvocationRecording,
    ResultEnvelope,
    ResultVerificationPolicy,
)


class EvaluationError(RuntimeError):
    """The evaluation suite cannot run as requested."""


class EvaluationNoCasesError(EvaluationError):
    """No case matches the requested selection."""


async def _eval_structured_contract(inputs: dict[str, Any]) -> MetricResult:
    schema = StructuredSchemaSpec.model_validate(inputs.get("schema") or {})
    payload = inputs.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("structured_contract requires a JSON-safe payload dict")
    return check_structured_contract(payload=payload, schema=schema)


async def _eval_evidence_completeness(inputs: dict[str, Any]) -> MetricResult:
    return check_evidence_completeness(
        cited_refs=tuple(inputs.get("cited_refs") or ()),
        available_refs=tuple(inputs.get("available_refs") or ()),
        min_evidence=int(inputs.get("min_evidence") or 1),
    )


async def _eval_result_admission(inputs: dict[str, Any]) -> MetricResult:
    result = ResultEnvelope.model_validate(inputs["result"])
    policy = ResultVerificationPolicy.model_validate(inputs["policy"])
    return check_result_admission(result=result, policy=policy)


async def _eval_replay_determinism(inputs: dict[str, Any]) -> MetricResult:
    recording = InvocationRecording.model_validate(inputs["recording"])
    return await check_replay_determinism(recording=recording)


async def _eval_red_action(inputs: dict[str, Any]) -> MetricResult:
    return check_red_action_blocked(
        allowed_tool_classes=tuple(inputs.get("allowed_tool_classes") or ()),
        allowed_effects=tuple(inputs.get("allowed_effects") or ()),
        attempted_tool_class=str(inputs.get("attempted_tool_class") or ""),
        required_effects=tuple(inputs.get("required_effects") or ()),
    )


_CHECKERS: dict[str, Callable[[dict[str, Any]], Awaitable[MetricResult]]] = {
    "structured_contract": _eval_structured_contract,
    "evidence_completeness": _eval_evidence_completeness,
    "result_admission": _eval_result_admission,
    "replay_determinism": _eval_replay_determinism,
    "red_action": _eval_red_action,
}


async def evaluate_case(case: EvaluationCase) -> CaseResult:
    """Run one case against its registered checker and derive a case status."""
    started_at = utc_now()
    checker = _CHECKERS.get(case.metric_id)
    if checker is None:
        metric = MetricResult(
            metric_id=case.metric_id,
            status=MetricStatus.ERROR,
            detail=f"unknown metric id: {case.metric_id}",
        )
    else:
        try:
            metric = await checker(case.inputs)
        except Exception as exc:
            metric = MetricResult(
                metric_id=case.metric_id,
                status=MetricStatus.ERROR,
                detail=_safe_error(exc),
            )
    completed_at = utc_now()
    status = _derive_status(metric)
    return CaseResult(
        case_key=case.key,
        title=case.title,
        metric_id=case.metric_id,
        status=status,
        metrics=(metric,),
        inputs=case.inputs,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=max(0.0, (completed_at - started_at).total_seconds()),
        error=metric.detail if status is CaseStatus.ERRORED else None,
    )


async def run_evaluation(
    *,
    cases: tuple[EvaluationCase, ...] | None = None,
    case_keys: tuple[str, ...] = (),
    mode: EvaluationMode = EvaluationMode.OFFLINE,
    reporter: EvaluationReporter | None = None,
) -> EvaluationReport:
    """Run the selected fixture cases and return a JSON-safe evaluation report.

    In ``OFFLINE`` mode no reporter is consulted.  In ``LIVE`` mode a reporter is
    used only when it is ``enabled``; reporting failures are swallowed so a broken
    observability adapter can never fail the evaluation itself.
    """
    selected = _select_cases(cases, case_keys)
    started_at = utc_now()
    results = [await evaluate_case(case) for case in selected]
    summary = _build_summary(results, started_at)
    run_id = derive_run_id(mode=mode, case_keys=tuple(case.key for case in selected))
    report = EvaluationReport(
        run_id=run_id,
        mode=mode,
        generated_at=utc_now(),
        summary=summary,
        cases=tuple(results),
    )
    if mode is EvaluationMode.LIVE and reporter is not None and reporter.enabled:
        reporter.report(report)
        report = report.model_copy(update={"reporter": reporter.name})
    return report


def _select_cases(
    cases: tuple[EvaluationCase, ...] | None,
    case_keys: tuple[str, ...],
) -> tuple[EvaluationCase, ...]:
    source = FIXTURE_CASES if cases is None else cases
    if not source:
        raise EvaluationNoCasesError("the evaluation suite contains no cases")
    if not case_keys:
        return tuple(source)
    by_key = {case.key: case for case in source}
    missing = [key for key in case_keys if key not in by_key]
    if missing:
        raise EvaluationNoCasesError(f"unknown case keys: {', '.join(sorted(missing))}")
    return tuple(by_key[key] for key in case_keys)


def _build_summary(results: list[CaseResult], started_at: datetime) -> EvaluationSummary:
    ended_at = utc_now()
    return EvaluationSummary(
        case_count=len(results),
        passed=sum(1 for result in results if result.status is CaseStatus.PASSED),
        failed=sum(1 for result in results if result.status is CaseStatus.FAILED),
        errored=sum(1 for result in results if result.status is CaseStatus.ERRORED),
        skipped=sum(1 for result in results if result.status is CaseStatus.SKIPPED),
        duration_seconds=max(0.0, (ended_at - started_at).total_seconds()),
    )


def _derive_status(metric: MetricResult) -> CaseStatus:
    if metric.status is MetricStatus.ERROR:
        return CaseStatus.ERRORED
    if metric.status is MetricStatus.FAIL:
        return CaseStatus.FAILED
    if metric.status is MetricStatus.SKIP:
        return CaseStatus.SKIPPED
    return CaseStatus.PASSED


def _safe_error(exc: BaseException) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message[:2_000]
