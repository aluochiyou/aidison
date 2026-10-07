from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aidison.runtime.run_health import (
    FailureClass,
    ProgressEvent,
    ProgressEventKind,
    ProgressWatchdogPolicy,
    RunHealth,
    evaluate_run_health,
)


def _event(
    kind: ProgressEventKind, *, seconds: int, invocation_hash: str | None = None
) -> ProgressEvent:
    return ProgressEvent(
        run_id=uuid4(),
        occurred_at=datetime.now(UTC) + timedelta(seconds=seconds),
        kind=kind,
        invocation_hash=invocation_hash,
    )


def test_watchdog_detects_repeated_invocations_without_meaningful_progress() -> None:
    run_id = uuid4()
    now = datetime.now(UTC)
    events = tuple(
        ProgressEvent(
            run_id=run_id,
            occurred_at=now + timedelta(seconds=index),
            kind=ProgressEventKind.INVOCATION_DISPATCHED,
            invocation_hash="same-query",
        )
        for index in range(3)
    )

    finding = evaluate_run_health(
        events, policy=ProgressWatchdogPolicy(max_duplicate_invocations=2)
    )

    assert finding.health is RunHealth.DUPLICATE_LOOP
    assert finding.failure_class is FailureClass.NO_PROGRESS


def test_watchdog_counts_admission_as_progress_and_leaves_healthy_run_alone() -> None:
    run_id = uuid4()
    now = datetime.now(UTC)
    events = (
        ProgressEvent(run_id=run_id, occurred_at=now, kind=ProgressEventKind.INVOCATION_DISPATCHED),
        ProgressEvent(
            run_id=run_id,
            occurred_at=now + timedelta(seconds=1),
            kind=ProgressEventKind.RESULT_ADMITTED,
        ),
    )

    finding = evaluate_run_health(events)

    assert finding.health is RunHealth.HEALTHY
    assert finding.failure_class is None


def test_watchdog_treats_user_wait_as_legitimate_wait_not_failure() -> None:
    finding = evaluate_run_health((_event(ProgressEventKind.USER_WAIT, seconds=0),))
    assert finding.health is RunHealth.WAITING_LEGITIMATELY
    assert finding.failure_class is FailureClass.USER_WAIT
