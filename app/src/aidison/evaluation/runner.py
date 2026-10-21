"""Deterministic offline evaluation runner (application layer).

The runner composes fixed fixture cases with pure metric checkers and returns a
JSON-safe ``EvaluationReport``.  It performs no external I/O itself; an optional
reporter is only consulted in ``LIVE`` mode and can never raise into the report.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aidison.application.agent_run_trajectory import AgentRunTrajectory
from aidison.evaluation.contracts import (
    CaseResult,
    CaseStatus,
    EvaluationCase,
    EvaluationMode,
    EvaluationReport,
    EvaluationSummary,
    FrozenFixtureManifest,
    MetricResult,
    MetricStatus,
    derive_run_id,
    utc_now,
)
from aidison.evaluation.fixtures import FIXTURE_CASES, FROZEN_FIXTURE_MANIFEST
from aidison.evaluation.metrics import (
    StructuredSchemaSpec,
    check_agent_run_regression_oracle,
    check_cost_latency_envelope,
    check_end_to_end_boundary,
    check_evidence_completeness,
    check_red_action_blocked,
    check_replay_bundle_readiness,
    check_replay_determinism,
    check_research_quality_projection,
    check_result_admission,
    check_structured_contract,
)
from aidison.evaluation.regression import AgentRunRegressionOracle
from aidison.evaluation.reporter import EvaluationReporter
from aidison.research.langgraph_contracts import AdmissionRecord, ResultEnvelope
from aidison.runtime.contracts import InvocationRecording
from aidison.runtime.replay_bundle import EvaluationReplayBundle


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
    admission = AdmissionRecord.model_validate(inputs["admission"])
    return check_result_admission(result=result, admission=admission)


async def _eval_replay_determinism(inputs: dict[str, Any]) -> MetricResult:
    recording = InvocationRecording.model_validate(inputs["recording"])
    return await check_replay_determinism(recording=recording)


async def _eval_replay_bundle_readiness(inputs: dict[str, Any]) -> MetricResult:
    bundle = EvaluationReplayBundle.model_validate(inputs["bundle"])
    return check_replay_bundle_readiness(bundle=bundle)


async def _eval_agent_run_regression_oracle(inputs: dict[str, Any]) -> MetricResult:
    return check_agent_run_regression_oracle(
        trajectory=AgentRunTrajectory.model_validate(inputs["trajectory"]),
        oracle=AgentRunRegressionOracle.model_validate(inputs["oracle"]),
    )


async def _eval_red_action(inputs: dict[str, Any]) -> MetricResult:
    return check_red_action_blocked(
        allowed_tool_classes=tuple(inputs.get("allowed_tool_classes") or ()),
        allowed_effects=tuple(inputs.get("allowed_effects") or ()),
        attempted_tool_class=str(inputs.get("attempted_tool_class") or ""),
        required_effects=tuple(inputs.get("required_effects") or ()),
    )


async def _eval_end_to_end_boundary(inputs: dict[str, Any]) -> MetricResult:
    return check_end_to_end_boundary(
        required_steps=tuple(inputs.get("required_steps") or ()),
        completed_steps=tuple(inputs.get("completed_steps") or ()),
        prohibited_steps=tuple(inputs.get("prohibited_steps") or ()),
    )


async def _eval_cost_latency_envelope(inputs: dict[str, Any]) -> MetricResult:
    return check_cost_latency_envelope(
        token_usage=int(inputs.get("token_usage") or 0),
        token_budget=int(inputs.get("token_budget") or 0),
        tool_calls=int(inputs.get("tool_calls") or 0),
        tool_call_budget=int(inputs.get("tool_call_budget") or 0),
        latency_seconds=float(inputs.get("latency_seconds") or 0.0),
        latency_budget_seconds=float(inputs.get("latency_budget_seconds") or 0.0),
    )


async def _eval_research_quality_projection(inputs: dict[str, Any]) -> MetricResult:
    return check_research_quality_projection(inputs=inputs)


_CHECKERS: dict[str, Callable[[dict[str, Any]], Awaitable[MetricResult]]] = {
    "structured_contract": _eval_structured_contract,
    "evidence_completeness": _eval_evidence_completeness,
    "result_admission": _eval_result_admission,
    "replay_determinism": _eval_replay_determinism,
    "replay_bundle_readiness": _eval_replay_bundle_readiness,
    "agent_run_regression_oracle": _eval_agent_run_regression_oracle,
    "red_action": _eval_red_action,
    "end_to_end_boundary": _eval_end_to_end_boundary,
    "cost_latency_envelope": _eval_cost_latency_envelope,
    "research_quality_projection": _eval_research_quality_projection,
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
        layer=case.layer,
        fixture_kind=case.fixture_kind,
        fixture_revision=case.fixture_revision,
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
    fixture_manifest = _fixture_manifest_for(cases=cases, selected=selected)
    run_id = derive_run_id(
        mode=mode,
        case_keys=tuple(case.key for case in selected),
        fixture_manifest_hash=fixture_manifest.content_hash,
    )
    report = EvaluationReport(
        run_id=run_id,
        mode=mode,
        generated_at=utc_now(),
        summary=summary,
        cases=tuple(results),
        fixture_manifest=fixture_manifest,
        layers_covered=tuple(sorted({case.layer for case in selected}, key=str)),
    )
    if mode is EvaluationMode.LIVE and reporter is not None and reporter.enabled:
        try:
            delivered = reporter.report(report)
        except Exception:
            # Reporters are integration boundaries.  A third-party or custom
            # adapter that violates the fail-safe contract cannot turn a local
            # deterministic verdict into an infrastructure failure.
            delivered = False
        if delivered:
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


def _fixture_manifest_for(
    *,
    cases: tuple[EvaluationCase, ...] | None,
    selected: tuple[EvaluationCase, ...],
) -> FrozenFixtureManifest:
    if cases is None and len(selected) == len(FIXTURE_CASES):
        return FROZEN_FIXTURE_MANIFEST
    return FrozenFixtureManifest.from_cases(
        fixture_set_key="adhoc-evaluation",
        fixture_set_revision="v1",
        cases=selected,
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
