from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.runtime.planning import (
    PlanTaskClaim,
    SchedulerSkip,
    SchedulerSkipReason,
    SchedulerTickResult,
    TaskClaimStatus,
    TaskDispatchIntent,
)


def _intent() -> TaskDispatchIntent:
    return TaskDispatchIntent(
        root_job_id=uuid4(),
        plan_revision=1,
        task_logical_key="research.a",
        claim_generation=1,
        graph_step_id="ready:research.a:claim1",
        task_kind="research",
        role_key="ready-worker",
        profile_id="ready-worker",
        profile_revision=1,
        basis_hash="a" * 64,
        shard_key="research.a",
        input_refs=("module://one",),
        token_budget=1_000,
        tool_call_budget=1,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def _intent_dict() -> dict[str, object]:
    return _intent().model_dump(mode="json")


def test_task_dispatch_intent_is_derived_from_frozen_facts() -> None:
    intent = _intent()
    assert intent.model_dump(mode="json")["basis_hash"] == "a" * 64
    assert intent.deadline.tzinfo is not None


def test_task_dispatch_intent_rejects_invalid_task_kind() -> None:
    with pytest.raises(ValidationError, match="task_kind"):
        TaskDispatchIntent.model_validate({**_intent_dict(), "task_kind": "Research.Bad"})


def test_task_dispatch_intent_rejects_non_hex_basis_hash() -> None:
    with pytest.raises(ValidationError, match="basis_hash"):
        TaskDispatchIntent.model_validate({**_intent_dict(), "basis_hash": "not-a-hash"})


def test_plan_task_claim_round_trips_with_nested_intent() -> None:
    intent = _intent()
    claim_id = uuid4()
    claim = PlanTaskClaim(
        claim_id=claim_id,
        root_job_id=intent.root_job_id,
        plan_revision=1,
        task_logical_key="research.a",
        claim_generation=1,
        lease_owner="scheduler-1",
        lease_token=uuid4(),
        lease_expires_at=intent.deadline,
        status=TaskClaimStatus.DISPATCHED,
        child_job_id=uuid4(),
        intent=intent,
        created_at=datetime.now(UTC),
    )
    restored = PlanTaskClaim.model_validate(claim.model_dump(mode="json"))
    assert restored == claim
    assert restored.intent.task_logical_key == "research.a"
    assert restored.status is TaskClaimStatus.DISPATCHED


def test_scheduler_tick_result_defaults_are_safe() -> None:
    result = SchedulerTickResult(root_job_id=uuid4(), plan_revision=1)
    assert result.dispatched == ()
    assert result.skipped == ()
    assert result.active_count == 0
    assert result.ready_remaining == 0
    assert result.complete is False


def test_scheduler_skip_carries_reason() -> None:
    skip = SchedulerSkip(
        task_logical_key="research.b",
        reason=SchedulerSkipReason.CONCURRENCY,
        detail="root active count reaches max_concurrency",
    )
    assert skip.reason is SchedulerSkipReason.CONCURRENCY
    with pytest.raises(ValidationError, match="reason"):
        SchedulerSkip(task_logical_key="research.b", reason="nope")


def test_claim_generation_and_lease_fields_are_positive() -> None:
    with pytest.raises(ValidationError, match="claim_generation"):
        TaskDispatchIntent.model_validate({**_intent_dict(), "claim_generation": 0})
    with pytest.raises(ValidationError, match="token_budget"):
        TaskDispatchIntent.model_validate({**_intent_dict(), "token_budget": 0})
