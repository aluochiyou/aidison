from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.tools.dispatcher import (
    DispatchAuthority,
    ToolCallIntent,
    ToolDispatchDenied,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolEffectClass,
    ToolProfileGrant,
    ToolRegistry,
    ToolRiskTier,
    ToolSpecRevision,
)


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    limit: int = Field(default=3, ge=1, le=5)


class PermissiveArguments(BaseModel):
    query: str


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        run_id=uuid4(),
        task_key="power.research",
        basis_hash="a" * 64,
        plan_revision=1,
        capability="research",
        input_refs=("project://basis",),
        dependency_task_ids=(),
        coverage_keys=("power.current",),
        allowed_tool_ids=("web_search",),
        budget_ref="budget://run/1",
        idempotency_key="task-power-research",
    )


def _dispatcher() -> ToolDispatcher:
    registry = ToolRegistry()
    registry.register(
        ToolSpecRevision(
            tool_id="web_search",
            revision=1,
            purpose="Find bounded public web sources.",
            tool_class="web_search",
            effect_class=ToolEffectClass.READ,
            risk_tier=ToolRiskTier.T1,
            input_schema_ref="aidison://schemas/web-search/v1",
            output_schema_ref="aidison://schemas/web-search-result/v1",
        ),
        input_model=SearchArguments,
    )
    return ToolDispatcher(registry)


def _authority(task: TaskEnvelope, *, t1_available: bool = True) -> DispatchAuthority:
    return DispatchAuthority(
        task=task,
        grant=ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=1,
            lease_token=uuid4(),
            deadline_ref="deadline://task/power",
            idempotency_prefix="run-1/task-power",
        ),
        profile=ToolProfileGrant(
            profile_ref="profile://research/v1",
            allowed_tool_ids=("web_search",),
            allowed_tool_classes=("web_search",),
            allowed_effect_classes=(ToolEffectClass.READ,),
        ),
        policy=ToolDispatchPolicy(
            allowed_tool_classes=("web_search",),
            allowed_effect_classes=(ToolEffectClass.READ,),
            available_risk_tiers=(
                (ToolRiskTier.T0, ToolRiskTier.T1)
                if t1_available
                else (ToolRiskTier.T0,)
            ),
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_dispatcher_authorizes_only_the_six_way_intersection_and_canonicalizes_args() -> None:
    task = _task()
    authorized = _dispatcher().authorize(
        ToolCallIntent(
            tool_call_id="call-1",
            tool_id="web_search",
            revision=1,
            arguments={"query": "motor current", "limit": 3},
            purpose_code="collect_specification",
        ),
        authority=_authority(task),
    )

    assert authorized.task_id == task.id
    assert authorized.canonical_arguments == {"query": "motor current", "limit": 3}
    assert authorized.logical_idempotency_key.startswith("run-1/task-power:web_search:1:")


def test_task_denial_prevents_profile_authorized_tool_from_becoming_a_call() -> None:
    task = _task().model_copy(update={"allowed_tool_ids": ()})

    with pytest.raises(ToolDispatchDenied, match="TaskEnvelope"):
        _dispatcher().authorize(
            ToolCallIntent(
                tool_call_id="call-1",
                tool_id="web_search",
                revision=1,
                arguments={"query": "motor current"},
                purpose_code="collect_specification",
            ),
            authority=_authority(task),
        )


def test_unavailable_tier_and_mismatched_grant_fail_closed() -> None:
    task = _task()
    intent = ToolCallIntent(
        tool_call_id="call-1",
        tool_id="web_search",
        revision=1,
        arguments={"query": "motor current"},
        purpose_code="collect_specification",
    )

    with pytest.raises(ToolDispatchDenied, match="risk tier"):
        _dispatcher().authorize(intent, authority=_authority(task, t1_available=False))

    authority = _authority(task).model_copy(
        update={"grant": _authority(_task()).grant}
    )
    with pytest.raises(ToolDispatchDenied, match="ExecutionGrant"):
        _dispatcher().authorize(intent, authority=authority)


def test_model_intent_cannot_supply_authority_fields_or_invalid_schema() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ToolCallIntent.model_validate(
            {
                "tool_call_id": "call-1",
                "tool_id": "web_search",
                "revision": 1,
                "arguments": {"query": "motor current"},
                "purpose_code": "collect_specification",
                "lease_token": str(uuid4()),
            }
        )

    task = _task()
    with pytest.raises(ToolDispatchDenied, match="input schema"):
        _dispatcher().authorize(
            ToolCallIntent(
                tool_call_id="call-1",
                tool_id="web_search",
                revision=1,
                arguments={"query": "motor current", "unexpected": True},
                purpose_code="collect_specification",
            ),
            authority=_authority(task),
        )


def test_registry_rejects_a_schema_that_silently_ignores_model_supplied_fields() -> None:
    with pytest.raises(ValueError, match="forbid undeclared"):
        ToolRegistry().register(
            ToolSpecRevision(
                tool_id="web_search",
                revision=1,
                purpose="Find bounded public web sources.",
                tool_class="web_search",
                effect_class=ToolEffectClass.READ,
                risk_tier=ToolRiskTier.T1,
                input_schema_ref="aidison://schemas/web-search/v1",
                output_schema_ref="aidison://schemas/web-search-result/v1",
            ),
            input_model=PermissiveArguments,
        )
