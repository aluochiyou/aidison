from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from aidison.evaluation import controlled_live
from aidison.evaluation.contracts import CaseStatus
from aidison.evaluation.controlled_live import (
    ControlledLiveSettings,
    ControlledLiveSuiteError,
    run_controlled_live_suite,
)
from aidison.evaluation.controlled_live_fixtures import (
    CONTROLLED_LIVE_CASES,
    CONTROLLED_LIVE_MANIFEST,
)

_PROJECT_ID = UUID("10000000-0000-0000-0000-000000000001")
_RESHAPE_ID = UUID("20000000-0000-0000-0000-000000000002")
_PLAN_ID = UUID("30000000-0000-0000-0000-000000000003")
_RUN_ID = UUID("40000000-0000-0000-0000-000000000004")
_DECISION_ID = UUID("50000000-0000-0000-0000-000000000005")
_BUDGET_ID = UUID("60000000-0000-0000-0000-000000000006")


def test_frozen_controlled_live_suite_has_six_content_addressed_cases() -> None:
    assert len(CONTROLLED_LIVE_CASES) == 6
    assert len({case.key for case in CONTROLLED_LIVE_CASES}) == 6
    assert CONTROLLED_LIVE_MANIFEST.content_hash == (
        "2ffd463fa3f054da8d217d7fce3a258bfeccb732ffcee27b695462eab54fe30f"
    )


def test_remote_origin_requires_explicit_https_authorization() -> None:
    with pytest.raises(ValidationError, match="allow_remote_base_url"):
        ControlledLiveSettings(base_url="https://aidison.example.com")
    with pytest.raises(ValidationError, match="must use HTTPS"):
        ControlledLiveSettings(
            base_url="http://aidison.example.com",
            allow_remote_base_url=True,
        )

    settings = ControlledLiveSettings(
        base_url="https://aidison.example.com",
        allow_remote_base_url=True,
    )

    assert settings.base_url == "https://aidison.example.com"


def test_provider_authorization_is_checked_before_network_io() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal requests
        requests += 1
        raise AssertionError("unauthorized controlled-live run must not contact the API")

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://127.0.0.1:8000",
        ) as client:
            with pytest.raises(ControlledLiveSuiteError, match="explicit"):
                await run_controlled_live_suite(
                    cases=(CONTROLLED_LIVE_CASES[0],),
                    settings=ControlledLiveSettings(),
                    client=client,
                )

    asyncio.run(run())
    assert requests == 0


def test_controlled_live_executes_full_governed_workflow_and_scores_result() -> None:
    captured: list[httpx.Request] = []
    handler = _workflow_handler(captured=captured)

    async def no_sleep(_: float) -> None:
        return None

    async def run() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://127.0.0.1:8000",
        ) as client:
            return await run_controlled_live_suite(
                cases=(CONTROLLED_LIVE_CASES[0],),
                settings=ControlledLiveSettings(allow_provider_calls=True),
                client=client,
                sleep=no_sleep,
                suite_run_id=UUID("70000000-0000-0000-0000-000000000007"),
            )

    report = asyncio.run(run())

    assert report.summary.all_passed
    assert report.summary.token_consumed == 1_234
    assert report.summary.tool_calls_consumed == 4
    result = report.cases[0]
    assert result.status is CaseStatus.PASSED
    assert result.phase.value == "evaluate"
    assert result.module_count == 3
    assert result.concept_coverage == 1
    assert result.plan_task_count == 3
    assert result.final_run_status == "waiting"
    assert result.pending_decision_count == 1
    assert result.supported_module_count == 1
    assert result.evidence_count == 3
    assert len(result.request_trace_ids) == 7
    assert not result.failures
    assert "api_key" not in json.dumps(report.model_dump(mode="json")).casefold()

    mutation_requests = [request for request in captured if request.method == "POST"]
    assert len(mutation_requests) == 6
    assert all(request.headers.get("Idempotency-Key") for request in mutation_requests)
    assert "If-Match" not in mutation_requests[0].headers
    assert all(request.headers.get("If-Match") for request in mutation_requests[1:])


def test_controlled_live_reports_quality_failures_without_hiding_runtime_success() -> None:
    captured: list[httpx.Request] = []
    handler = _workflow_handler(captured=captured, admissible=False)

    async def run() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://127.0.0.1:8000",
        ) as client:
            return await run_controlled_live_suite(
                cases=(CONTROLLED_LIVE_CASES[0],),
                settings=ControlledLiveSettings(allow_provider_calls=True),
                client=client,
                suite_run_id=UUID("80000000-0000-0000-0000-000000000008"),
            )

    report = asyncio.run(run())

    assert report.summary.failed == 1
    result = report.cases[0]
    assert result.status is CaseStatus.FAILED
    assert result.final_run_status == "waiting"
    assert "proposal_preview_not_admissible" in result.failures


def test_cli_lists_cases_without_authorizing_provider_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert controlled_live.main(["--list-cases"]) == 0
    output = capsys.readouterr().out
    assert "balanced-quadrotor" in output
    assert CONTROLLED_LIVE_MANIFEST.content_hash in output


def test_cli_requires_case_selection_and_provider_acknowledgement() -> None:
    with pytest.raises(SystemExit, match="2"):
        controlled_live.main([])
    with pytest.raises(SystemExit, match="2"):
        controlled_live.main(["--case", "balanced-quadrotor"])


def _workflow_handler(
    *,
    captured: list[httpx.Request],
    admissible: bool = True,
) -> Callable[[httpx.Request], httpx.Response]:
    counter = 0

    def response(status_code: int, payload: object) -> httpx.Response:
        nonlocal counter
        counter += 1
        return httpx.Response(
            status_code,
            json=payload,
            headers={"X-Request-ID": f"trace-{counter}"},
        )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and path == "/api/projects":
            return response(201, {"id": str(_PROJECT_ID), "revision": 1})
        if request.method == "POST" and path.endswith("/requirements"):
            return response(200, {"project_revision": 2})
        if request.method == "POST" and path.endswith("/module-discovery"):
            return response(
                201,
                {
                    "reshape_proposal": {"id": str(_RESHAPE_ID)},
                    "project_revision": 2,
                },
            )
        if request.method == "POST" and path == f"/api/reshape-proposals/{_RESHAPE_ID}/resolve":
            return response(200, {"project_revision": 3})
        if request.method == "POST" and path.endswith("/execution-plans/research-strategy"):
            return response(
                201,
                {
                    "execution_plan": {
                        "id": str(_PLAN_ID),
                        "scope_hash": "a" * 64,
                        "research_strategy": {
                            "tasks": [{"task_key": f"task-{index}"} for index in range(3)]
                        },
                    },
                    "project_revision": 3,
                },
            )
        if request.method == "POST" and path == f"/api/execution-plans/{_PLAN_ID}/resolve":
            return response(200, {"agent_run": {"id": str(_RUN_ID)}, "project_revision": 3})
        if request.method == "GET" and path.endswith("/snapshot"):
            return response(200, _snapshot(admissible=admissible))
        return httpx.Response(404, json={"detail": f"unexpected route: {request.method} {path}"})

    return handler


def _snapshot(*, admissible: bool) -> dict[str, object]:
    return {
        "modules": [
            {"name": "动力系统", "responsibility": "电机与螺旋桨选型"},
            {"name": "电源系统", "responsibility": "电池与供电预算"},
            {"name": "飞控系统", "responsibility": "飞控与传感器集成"},
        ],
        "agent_runs": [{"id": str(_RUN_ID), "status": "waiting"}],
        "agent_run_decisions": [
            {
                "id": str(_DECISION_ID),
                "agent_run_id": str(_RUN_ID),
                "status": "pending",
                "proposal_preview": {
                    "adoption_allowed": admissible,
                    "module_summaries": [
                        {
                            "module_id": "module-1",
                            "recommended_option": "candidate-a",
                            "evidence_count": 3,
                        }
                    ],
                },
            }
        ],
        "agent_run_budget_accounts": [
            {
                "id": str(_BUDGET_ID),
                "agent_run_id": str(_RUN_ID),
                "token_consumed": 1_234,
                "tool_calls_consumed": 4,
            }
        ],
    }
