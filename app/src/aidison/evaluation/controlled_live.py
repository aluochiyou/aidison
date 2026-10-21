"""Opt-in real-provider benchmark for the complete Aidison research workflow.

The ordinary evaluation CLI remains deterministic and offline.  This module is
separate because it creates durable projects and consumes model/search quota.
It refuses to run unless the operator selects cases and explicitly authorizes
provider calls.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aidison.evaluation.contracts import CaseStatus
from aidison.evaluation.controlled_live_contracts import (
    ControlledLiveCase,
    ControlledLiveCaseResult,
    ControlledLiveManifest,
    ControlledLivePhase,
    ControlledLiveReport,
    ControlledLiveSummary,
)
from aidison.evaluation.controlled_live_fixtures import (
    CONTROLLED_LIVE_CASES,
    CONTROLLED_LIVE_MANIFEST,
)

_TERMINAL_RUN_STATUSES = frozenset({"waiting", "succeeded", "failed", "cancelled"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ControlledLiveSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = "http://127.0.0.1:8000"
    allow_provider_calls: bool = False
    allow_remote_base_url: bool = False
    request_timeout_seconds: float = Field(default=150, gt=0, le=600)
    poll_interval_seconds: float = Field(default=2, gt=0, le=30)
    case_timeout_seconds: float = Field(default=1_200, ge=60, le=7_200)

    @field_validator("base_url")
    @classmethod
    def base_url_is_origin_without_credentials(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("controlled-live base URL must be a credential-free HTTP origin")
        return value.rstrip("/")

    @model_validator(mode="after")
    def remote_origin_is_explicit_and_encrypted(self) -> ControlledLiveSettings:
        parsed = urlsplit(self.base_url)
        is_loopback = _is_loopback_host(parsed.hostname or "")
        if not is_loopback and not self.allow_remote_base_url:
            raise ValueError("remote controlled-live origins require allow_remote_base_url")
        if not is_loopback and parsed.scheme != "https":
            raise ValueError("remote controlled-live origins must use HTTPS")
        return self


class ControlledLiveSuiteError(RuntimeError):
    pass


class ControlledLiveHttpError(ControlledLiveSuiteError):
    def __init__(self, *, phase: ControlledLivePhase, status_code: int, detail: str) -> None:
        self.phase = phase
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{phase.value} returned HTTP {status_code}: {detail}")


class _CaseExecutionState:
    def __init__(self) -> None:
        self.phase = ControlledLivePhase.PREFLIGHT
        self.project_id: UUID | None = None
        self.agent_run_id: UUID | None = None
        self.project_revision: int | None = None
        self.module_count = 0
        self.concept_groups_matched = 0
        self.concept_group_count = 0
        self.concept_coverage = 0.0
        self.plan_task_count = 0
        self.final_run_status: str | None = None
        self.pending_decision_count = 0
        self.supported_module_count = 0
        self.evidence_count = 0
        self.token_consumed = 0
        self.tool_calls_consumed = 0
        self.trace_ids: list[str] = []


async def run_controlled_live_suite(
    *,
    cases: tuple[ControlledLiveCase, ...],
    settings: ControlledLiveSettings,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    suite_run_id: UUID | None = None,
) -> ControlledLiveReport:
    """Run selected cases sequentially so configured budgets remain meaningful."""

    if not settings.allow_provider_calls:
        raise ControlledLiveSuiteError(
            "controlled-live execution requires explicit allow_provider_calls authorization"
        )
    if not cases:
        raise ControlledLiveSuiteError("controlled-live execution requires at least one case")
    if len({case.key for case in cases}) != len(cases):
        raise ControlledLiveSuiteError("controlled-live case keys must be unique")

    run_id = suite_run_id or uuid4()
    started_at = _utc_now()
    owns_client = client is None
    selected_client = client or httpx.AsyncClient(
        base_url=settings.base_url,
        timeout=httpx.Timeout(settings.request_timeout_seconds),
        follow_redirects=False,
        headers={"User-Agent": "aidison-controlled-live/1"},
    )
    try:
        await _preflight(selected_client)
        results = []
        for case in cases:
            results.append(
                await _run_case(
                    case=case,
                    suite_run_id=run_id,
                    settings=settings,
                    client=selected_client,
                    sleep=sleep,
                )
            )
    finally:
        if owns_client:
            await selected_client.aclose()

    completed_at = _utc_now()
    manifest = ControlledLiveManifest.from_cases(cases)
    summary = ControlledLiveSummary(
        case_count=len(results),
        passed=sum(item.status is CaseStatus.PASSED for item in results),
        failed=sum(item.status is CaseStatus.FAILED for item in results),
        errored=sum(item.status is CaseStatus.ERRORED for item in results),
        duration_seconds=max(0, (completed_at - started_at).total_seconds()),
        token_consumed=sum(item.token_consumed for item in results),
        tool_calls_consumed=sum(item.tool_calls_consumed for item in results),
    )
    return ControlledLiveReport(
        suite_run_id=run_id,
        base_url=settings.base_url,
        manifest=manifest,
        started_at=started_at,
        completed_at=completed_at,
        summary=summary,
        cases=tuple(results),
    )


async def _preflight(client: httpx.AsyncClient) -> None:
    try:
        response = await client.get("/health")
    except httpx.HTTPError as exc:
        raise ControlledLiveSuiteError(
            f"controlled-live preflight failed: {_safe_error(exc)}"
        ) from exc
    if response.status_code != 200:
        raise ControlledLiveHttpError(
            phase=ControlledLivePhase.PREFLIGHT,
            status_code=response.status_code,
            detail=_safe_response_detail(response),
        )


async def _run_case(
    *,
    case: ControlledLiveCase,
    suite_run_id: UUID,
    settings: ControlledLiveSettings,
    client: httpx.AsyncClient,
    sleep: Callable[[float], Awaitable[None]],
) -> ControlledLiveCaseResult:
    started_at = _utc_now()
    state = _CaseExecutionState()
    try:
        state.phase = ControlledLivePhase.CREATE_PROJECT
        created = await _request_json(
            client=client,
            method="POST",
            path="/api/projects",
            phase=state.phase,
            trace_ids=state.trace_ids,
            json_body={
                "name": f"{case.project_name} [{str(suite_run_id)[:8]}]"[:160],
                "goal": case.goal,
            },
            idempotency_key=_idempotency_key(suite_run_id, case.key, state.phase),
            expected_status=201,
        )
        state.project_id = UUID(_required_string(created, "id", state.phase))
        state.project_revision = _required_int(created, "revision", state.phase)

        state.phase = ControlledLivePhase.APPROVE_REQUIREMENTS
        requirements = await _request_json(
            client=client,
            method="POST",
            path=f"/api/projects/{state.project_id}/requirements",
            phase=state.phase,
            trace_ids=state.trace_ids,
            json_body={
                "goal": case.goal,
                **case.requirements.model_dump(mode="json"),
            },
            idempotency_key=_idempotency_key(suite_run_id, case.key, state.phase),
            revision=state.project_revision,
        )
        state.project_revision = _required_int(requirements, "project_revision", state.phase)

        state.phase = ControlledLivePhase.DISCOVER_MODULES
        discovery = await _request_json(
            client=client,
            method="POST",
            path=f"/api/projects/{state.project_id}/module-discovery",
            phase=state.phase,
            trace_ids=state.trace_ids,
            json_body={"planning_brief": case.planning_brief, "structure_depth": "deep"},
            idempotency_key=_idempotency_key(suite_run_id, case.key, state.phase),
            revision=state.project_revision,
            expected_status=201,
        )
        reshape = _required_mapping(discovery, "reshape_proposal", state.phase)
        reshape_id = UUID(_required_string(reshape, "id", state.phase))

        state.phase = ControlledLivePhase.APPLY_MODULE_STRUCTURE
        resolved = await _request_json(
            client=client,
            method="POST",
            path=f"/api/reshape-proposals/{reshape_id}/resolve",
            phase=state.phase,
            trace_ids=state.trace_ids,
            json_body={"decision": "applied"},
            idempotency_key=_idempotency_key(suite_run_id, case.key, state.phase),
            revision=state.project_revision,
        )
        state.project_revision = _required_int(resolved, "project_revision", state.phase)

        state.phase = ControlledLivePhase.PLAN_RESEARCH
        planned = await _request_json(
            client=client,
            method="POST",
            path=f"/api/projects/{state.project_id}/execution-plans/research-strategy",
            phase=state.phase,
            trace_ids=state.trace_ids,
            json_body={
                "objective": case.objective,
                "requires_independent_verification": case.research_depth != "focused",
                "max_concurrency": case.max_concurrency,
                "max_token_budget": case.max_token_budget,
                "max_duration_seconds": case.max_duration_seconds,
                "research_depth": case.research_depth,
                "source_strategy": case.source_strategy,
            },
            idempotency_key=_idempotency_key(suite_run_id, case.key, state.phase),
            revision=state.project_revision,
            expected_status=201,
        )
        execution_plan = _required_mapping(planned, "execution_plan", state.phase)
        execution_plan_id = UUID(_required_string(execution_plan, "id", state.phase))
        scope_hash = _required_string(execution_plan, "scope_hash", state.phase)
        strategy = _required_mapping(execution_plan, "research_strategy", state.phase)
        tasks = strategy.get("tasks")
        if not isinstance(tasks, list):
            raise ControlledLiveSuiteError("plan_research response has no task list")
        state.plan_task_count = len(tasks)

        state.phase = ControlledLivePhase.ENQUEUE_RESEARCH
        enqueued = await _request_json(
            client=client,
            method="POST",
            path=f"/api/execution-plans/{execution_plan_id}/resolve",
            phase=state.phase,
            trace_ids=state.trace_ids,
            json_body={
                "decision": "approved",
                "scope_hash": scope_hash,
                "enqueue_research": True,
            },
            idempotency_key=_idempotency_key(suite_run_id, case.key, state.phase),
            revision=state.project_revision,
        )
        agent_run = _required_mapping(enqueued, "agent_run", state.phase)
        state.agent_run_id = UUID(_required_string(agent_run, "id", state.phase))

        state.phase = ControlledLivePhase.AWAIT_RUN
        snapshot = await _await_terminal_snapshot(
            client=client,
            project_id=state.project_id,
            agent_run_id=state.agent_run_id,
            deadline=monotonic() + settings.case_timeout_seconds,
            poll_interval_seconds=settings.poll_interval_seconds,
            trace_ids=state.trace_ids,
            sleep=sleep,
        )
        state.phase = ControlledLivePhase.EVALUATE
        failures = _evaluate_snapshot(case=case, snapshot=snapshot, state=state)
        completed_at = _utc_now()
        return _case_result(
            case=case,
            state=state,
            status=CaseStatus.FAILED if failures else CaseStatus.PASSED,
            failures=failures,
            started_at=started_at,
            completed_at=completed_at,
        )
    except Exception as exc:
        completed_at = _utc_now()
        return _case_result(
            case=case,
            state=state,
            status=CaseStatus.ERRORED,
            failures=(),
            started_at=started_at,
            completed_at=completed_at,
            error=_safe_error(exc),
        )


async def _await_terminal_snapshot(
    *,
    client: httpx.AsyncClient,
    project_id: UUID,
    agent_run_id: UUID,
    deadline: float,
    poll_interval_seconds: float,
    trace_ids: list[str],
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, Any]:
    while monotonic() < deadline:
        snapshot = await _request_json(
            client=client,
            method="GET",
            path=f"/api/projects/{project_id}/snapshot",
            phase=ControlledLivePhase.AWAIT_RUN,
            trace_ids=trace_ids,
        )
        run = _find_by_id(snapshot.get("agent_runs"), agent_run_id)
        if run is not None and run.get("status") in _TERMINAL_RUN_STATUSES:
            return snapshot
        await sleep(poll_interval_seconds)
    raise ControlledLiveSuiteError("controlled-live AgentRun exceeded the case timeout")


def _evaluate_snapshot(
    *,
    case: ControlledLiveCase,
    snapshot: dict[str, Any],
    state: _CaseExecutionState,
) -> tuple[str, ...]:
    modules = snapshot.get("modules")
    module_rows = modules if isinstance(modules, list) else []
    state.module_count = len(module_rows)
    corpus = json.dumps(module_rows, ensure_ascii=False, sort_keys=True).casefold()
    state.concept_group_count = len(case.concept_groups)
    state.concept_groups_matched = sum(
        any(term.casefold() in corpus for term in group) for group in case.concept_groups
    )
    state.concept_coverage = state.concept_groups_matched / state.concept_group_count

    run = _find_by_id(snapshot.get("agent_runs"), state.agent_run_id)
    state.final_run_status = str(run.get("status")) if run is not None else None
    decisions = [
        item
        for item in _mapping_items(snapshot.get("agent_run_decisions"))
        if item.get("agent_run_id") == str(state.agent_run_id) and item.get("status") == "pending"
    ]
    state.pending_decision_count = len(decisions)
    previews = [item.get("proposal_preview") for item in decisions]
    valid_previews = [item for item in previews if isinstance(item, dict)]
    module_summaries = [
        summary
        for preview in valid_previews
        for summary in preview.get("module_summaries", [])
        if isinstance(summary, dict)
    ]
    state.supported_module_count = len(module_summaries)
    state.evidence_count = sum(
        int(summary.get("evidence_count", 0))
        for summary in module_summaries
        if isinstance(summary.get("evidence_count", 0), int)
    )

    budget = _find_by_key(
        snapshot.get("agent_run_budget_accounts"), "agent_run_id", state.agent_run_id
    )
    if budget is not None:
        state.token_consumed = _non_negative_int(budget.get("token_consumed"))
        state.tool_calls_consumed = _non_negative_int(budget.get("tool_calls_consumed"))

    failures: list[str] = []
    if state.module_count < case.min_module_count:
        failures.append("module_count_below_minimum")
    if state.concept_coverage < case.min_concept_coverage:
        failures.append("module_concept_coverage_below_minimum")
    if state.plan_task_count < case.min_plan_task_count:
        failures.append("plan_task_count_below_minimum")
    if state.final_run_status != "waiting":
        failures.append(f"agent_run_did_not_wait_for_approval:{state.final_run_status}")
    if state.pending_decision_count != 1:
        failures.append("pending_result_decision_count_not_one")
    if state.supported_module_count < case.min_supported_modules:
        failures.append("supported_module_count_below_minimum")
    if state.evidence_count < case.min_evidence_count:
        failures.append("evidence_count_below_minimum")
    if not valid_previews or any(not preview.get("adoption_allowed") for preview in valid_previews):
        failures.append("proposal_preview_not_admissible")
    if budget is None:
        failures.append("agent_run_budget_account_missing")
    elif state.token_consumed > case.max_token_budget:
        failures.append("token_budget_exceeded")
    return tuple(failures)


def _case_result(
    *,
    case: ControlledLiveCase,
    state: _CaseExecutionState,
    status: CaseStatus,
    failures: tuple[str, ...],
    started_at: datetime,
    completed_at: datetime,
    error: str | None = None,
) -> ControlledLiveCaseResult:
    return ControlledLiveCaseResult(
        case_key=case.key,
        title=case.title,
        status=status,
        phase=state.phase,
        project_id=state.project_id,
        agent_run_id=state.agent_run_id,
        project_revision=state.project_revision,
        module_count=state.module_count,
        concept_groups_matched=state.concept_groups_matched,
        concept_group_count=state.concept_group_count,
        concept_coverage=state.concept_coverage,
        plan_task_count=state.plan_task_count,
        final_run_status=state.final_run_status,
        pending_decision_count=state.pending_decision_count,
        supported_module_count=state.supported_module_count,
        evidence_count=state.evidence_count,
        token_consumed=state.token_consumed,
        tool_calls_consumed=state.tool_calls_consumed,
        failures=failures,
        request_trace_ids=tuple(state.trace_ids),
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=max(0, (completed_at - started_at).total_seconds()),
        error=error,
    )


async def _request_json(
    *,
    client: httpx.AsyncClient,
    method: str,
    path: str,
    phase: ControlledLivePhase,
    trace_ids: list[str],
    json_body: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    revision: int | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    headers = {}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    try:
        response = await client.request(method, path, json=json_body, headers=headers)
    except httpx.HTTPError as exc:
        raise ControlledLiveSuiteError(f"{phase.value} request failed: {_safe_error(exc)}") from exc
    trace_id = response.headers.get("X-Request-ID")
    if trace_id:
        trace_ids.append(trace_id[:200])
    if response.status_code != expected_status:
        raise ControlledLiveHttpError(
            phase=phase,
            status_code=response.status_code,
            detail=_safe_response_detail(response),
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ControlledLiveSuiteError(f"{phase.value} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ControlledLiveSuiteError(f"{phase.value} response must be a JSON object")
    return payload


def _required_mapping(
    value: dict[str, Any], key: str, phase: ControlledLivePhase
) -> dict[str, Any]:
    selected = value.get(key)
    if not isinstance(selected, dict):
        raise ControlledLiveSuiteError(f"{phase.value} response is missing {key}")
    return selected


def _required_string(value: dict[str, Any], key: str, phase: ControlledLivePhase) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise ControlledLiveSuiteError(f"{phase.value} response is missing {key}")
    return selected


def _required_int(value: dict[str, Any], key: str, phase: ControlledLivePhase) -> int:
    selected = value.get(key)
    if not isinstance(selected, int) or isinstance(selected, bool) or selected < 1:
        raise ControlledLiveSuiteError(f"{phase.value} response is missing {key}")
    return selected


def _mapping_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _find_by_id(value: object, selected_id: UUID | None) -> dict[str, Any] | None:
    return _find_by_key(value, "id", selected_id)


def _find_by_key(value: object, key: str, selected: UUID | None) -> dict[str, Any] | None:
    expected = str(selected) if selected is not None else None
    return next((item for item in _mapping_items(value) if item.get(key) == expected), None)


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _idempotency_key(
    suite_run_id: UUID, case_key: str, phase: ControlledLivePhase
) -> str:
    return f"controlled-live:{suite_run_id.hex}:{case_key}:{phase.value}"


def _safe_response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:1_000]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            return f"{code}: {message}"[:1_000]
        detail = payload.get("detail")
        if detail is not None:
            return str(detail)[:1_000]
    return str(payload)[:1_000]


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2_000]


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _select_cases(case_keys: Sequence[str], all_cases: bool) -> tuple[ControlledLiveCase, ...]:
    if all_cases:
        return CONTROLLED_LIVE_CASES
    by_key = {case.key: case for case in CONTROLLED_LIVE_CASES}
    missing = sorted(set(case_keys) - by_key.keys())
    if missing:
        raise ControlledLiveSuiteError(f"unknown controlled-live cases: {', '.join(missing)}")
    return tuple(by_key[key] for key in dict.fromkeys(case_keys))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aidison.evaluation.controlled_live",
        description="Run the opt-in provider-backed Aidison controlled-live benchmark",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case", action="append", default=[], help="case key; repeatable")
    selection.add_argument("--all-cases", action="store_true", help="run all frozen cases")
    parser.add_argument("--list-cases", action="store_true", help="list cases without network I/O")
    parser.add_argument(
        "--allow-provider-calls",
        action="store_true",
        help="required acknowledgement that this run consumes model/search quota",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--allow-remote-base-url",
        action="store_true",
        help="allow an explicit HTTPS non-loopback API origin",
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=150)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--case-timeout-seconds", type=float, default=1_200)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-failure", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.list_cases:
        for case in CONTROLLED_LIVE_CASES:
            print(f"{case.key}\t{case.title}\tsha256:{case.content_hash}")
        print(f"manifest\tsha256:{CONTROLLED_LIVE_MANIFEST.content_hash}")
        return 0
    if not args.case and not args.all_cases:
        parser.error("select at least one --case or use --all-cases")
    if not args.allow_provider_calls:
        parser.error("--allow-provider-calls is required because this benchmark consumes quota")
    try:
        cases = _select_cases(args.case, args.all_cases)
        settings = ControlledLiveSettings(
            base_url=args.base_url,
            allow_provider_calls=args.allow_provider_calls,
            allow_remote_base_url=args.allow_remote_base_url,
            request_timeout_seconds=args.request_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            case_timeout_seconds=args.case_timeout_seconds,
        )
        report = asyncio.run(run_controlled_live_suite(cases=cases, settings=settings))
    except (ControlledLiveSuiteError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = report.model_dump(mode="json")
    if args.report is not None:
        _write_report(args.report, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    summary = report.summary
    print(
        "controlled-live: "
        f"{summary.passed} passed, {summary.failed} failed, {summary.errored} errored; "
        f"tokens={summary.token_consumed}, tool_calls={summary.tool_calls_consumed}"
    )
    if summary.all_passed or args.allow_failure:
        return 0
    return 1


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ControlledLiveHttpError",
    "ControlledLiveSettings",
    "ControlledLiveSuiteError",
    "main",
    "run_controlled_live_suite",
]
