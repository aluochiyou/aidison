from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aidison.evaluation.trajectory import (
    AblationArm,
    AblationBudget,
    AblationQuality,
    TrajectoryBinding,
    TrajectoryEvent,
    TrajectoryEventKind,
    TrajectoryInvocation,
    build_trajectory_export,
    compare_iso_budget,
)
from aidison.runtime.agent_run_budget import AgentRunBudgetOperationKind

_HASH = "a" * 64
_HASH_TWO = "b" * 64
_NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _binding() -> TrajectoryBinding:
    return TrajectoryBinding(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        basis_hash=_HASH,
        graph_revision="research-graph/r4",
        state_schema_version="research-state/v1",
        profile_binding_hash=_HASH,
        policy_binding_hash=_HASH_TWO,
    )


def _invocation(**changes: object) -> TrajectoryInvocation:
    payload: dict[str, object] = {
        "invocation_id": uuid4(),
        "occurred_at": _NOW,
        "module_key": "power",
        "task_key": "power/motor-research",
        "kind": AgentRunBudgetOperationKind.MODEL,
        "provider": "openai",
        "target": "gpt-5.6",
        "request_hash": _HASH,
        "physical_attempt_no": 1,
        "token_usage": 100,
        "tool_call_usage": 0,
        "latency_ms": 250,
        "result_admitted": True,
    }
    payload.update(changes)
    return TrajectoryInvocation.model_validate(payload)


def test_trajectory_export_is_secret_free_and_aggregates_cost_by_module_task_and_operation() -> (
    None
):
    export = build_trajectory_export(
        binding=_binding(),
        invocations=(
            _invocation(),
            _invocation(
                physical_attempt_no=2,
                token_usage=40,
                latency_ms=120,
                result_admitted=False,
            ),
            _invocation(
                module_key="structure",
                task_key="structure/mounting",
                kind=AgentRunBudgetOperationKind.TOOL,
                provider="tavily",
                target="search",
                request_hash=_HASH_TWO,
                token_usage=0,
                tool_call_usage=1,
                latency_ms=90,
                result_admitted=True,
            ),
        ),
        events=(
            TrajectoryEvent(
                occurred_at=_NOW + timedelta(milliseconds=250),
                kind=TrajectoryEventKind.RESULT_ADMITTED,
                task_key="power/motor-research",
            ),
        ),
    )
    assert export.summary.total_tokens == 140
    assert export.summary.total_tool_calls == 1
    assert export.summary.total_latency_ms == 460
    assert export.summary.retry_count == 1
    assert export.summary.cost_without_admitted_result_tokens == 40
    assert export.summary.admitted_result_count == 2
    assert [(item.module_key, item.task_key) for item in export.attribution] == [
        ("power", "power/motor-research"),
        ("structure", "structure/mounting"),
    ]
    assert len(export.content_hash) == 64


def test_trajectory_contract_rejects_hidden_prompt_fields_and_cross_run_event_data() -> None:
    with pytest.raises(ValidationError):
        TrajectoryInvocation.model_validate({**_invocation().model_dump(), "raw_prompt": "secret"})
    with pytest.raises(ValueError, match="same AgentRun"):
        build_trajectory_export(
            binding=_binding(),
            invocations=(_invocation(),),
            events=(
                TrajectoryEvent(
                    occurred_at=_NOW,
                    kind=TrajectoryEventKind.RESULT_ADMITTED,
                    run_id=uuid4(),
                ),
            ),
        )


def _budget(**changes: object) -> AblationBudget:
    payload: dict[str, object] = {
        "fixture_manifest_hash": _HASH,
        "model_binding_hash": _HASH_TWO,
        "tool_policy_hash": "c" * 64,
        "token_cap": 800,
        "tool_call_cap": 4,
        "max_latency_ms": 10_000,
        "concurrency_cap": 3,
    }
    payload.update(changes)
    return AblationBudget.model_validate(payload)


def _arm(**changes: object) -> AblationArm:
    payload: dict[str, object] = {
        "arm_key": "single",
        "run_id": UUID("00000000-0000-0000-0000-000000000010"),
        "budget": _budget(),
        "quality": AblationQuality(
            coverage_completion=0.8,
            unsupported_claim_rate=0.1,
            conflict_recall=0.5,
        ),
        "observed_tokens": 600,
        "observed_tool_calls": 3,
        "observed_latency_ms": 6_000,
    }
    payload.update(changes)
    return AblationArm.model_validate(payload)


def test_iso_budget_comparison_rejects_unmatched_budgets_and_computes_pareto_frontier() -> None:
    single = _arm()
    adaptive = _arm(
        arm_key="adaptive",
        run_id=UUID("00000000-0000-0000-0000-000000000011"),
        quality=AblationQuality(
            coverage_completion=0.9,
            unsupported_claim_rate=0.05,
            conflict_recall=0.8,
        ),
        observed_tokens=590,
        observed_tool_calls=3,
        observed_latency_ms=5_500,
    )
    comparison = compare_iso_budget((single, adaptive))
    assert comparison.is_iso_budget is True
    assert comparison.pareto_arm_keys == ("adaptive",)
    with pytest.raises(ValueError, match="same frozen budget"):
        compare_iso_budget((_arm(), _arm(arm_key="other", budget=_budget(token_cap=900))))


def test_ablation_arm_cannot_claim_results_outside_its_budget() -> None:
    with pytest.raises(ValidationError, match="token cap"):
        _arm(observed_tokens=801)
