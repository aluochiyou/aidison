from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from aidison.infrastructure.runtime import _evaluate_open_join
from aidison.runtime.contracts import DelegationStatus, JoinMode, JoinPolicy


@pytest.mark.parametrize(
    ("mode", "min_successes", "statuses", "deadline_reached", "ready", "impossible"),
    (
        (JoinMode.ALL_REQUIRED, 2, ("pending", "pending"), False, False, False),
        (JoinMode.ALL_REQUIRED, 2, ("succeeded", "succeeded"), False, True, False),
        (JoinMode.ALL_REQUIRED, 2, ("succeeded", "failed"), False, False, True),
        (JoinMode.BOUNDED_PARTIAL, 2, ("succeeded", "succeeded", "pending"), False, False, False),
        (JoinMode.BOUNDED_PARTIAL, 2, ("succeeded", "succeeded", "pending"), True, True, False),
        (JoinMode.BOUNDED_PARTIAL, 2, ("succeeded", "failed", "cancelled"), False, False, True),
        (JoinMode.FIRST_VALID, 1, ("succeeded", "pending"), False, True, False),
        (JoinMode.FIRST_VALID, 1, ("pending", "pending"), False, False, False),
        (JoinMode.FIRST_VALID, 1, ("failed", "cancelled"), False, False, True),
        (JoinMode.FIRST_VALID, 1, ("pending", "pending"), True, False, True),
    ),
)
def test_join_policy_truth_table(
    mode: JoinMode,
    min_successes: int,
    statuses: tuple[str, ...],
    deadline_reached: bool,
    ready: bool,
    impossible: bool,
) -> None:
    now = datetime.now(UTC)
    delegation_ids = tuple(uuid4() for _ in statuses)
    policy = JoinPolicy(
        mode=mode,
        expected_delegation_ids=delegation_ids,
        min_successes=min_successes,
        deadline=now - timedelta(seconds=1) if deadline_reached else now + timedelta(minutes=1),
    )
    delegations = [
        cast(
            Any,
            SimpleNamespace(
                id=delegation_id,
                status=status,
                completed_at=now if status == DelegationStatus.SUCCEEDED.value else None,
            ),
        )
        for delegation_id, status in zip(delegation_ids, statuses, strict=True)
    ]

    _, actual_ready, actual_impossible, actual_deadline = _evaluate_open_join(
        policy=policy,
        delegations=delegations,
        now=now,
    )

    assert actual_ready is ready
    assert actual_impossible is impossible
    assert actual_deadline is deadline_reached
