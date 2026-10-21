"""Typed failure classification and deterministic meaningful-progress watchdog."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_RESULT = "invalid_result"
    STALE_GENERATION = "stale_generation"
    AMBIGUOUS_EFFECT = "ambiguous_effect"
    INCOMPATIBLE_STATE = "incompatible_state"
    NO_PROGRESS = "no_progress"
    USER_WAIT = "user_wait"


class ProgressEventKind(StrEnum):
    INVOCATION_DISPATCHED = "invocation_dispatched"
    RESULT_ADMITTED = "result_admitted"
    COVERAGE_CHANGED = "coverage_changed"
    CONFLICT_CHANGED = "conflict_changed"
    TASK_UNLOCKED = "task_unlocked"
    USER_DECISION_CONSUMED = "user_decision_consumed"
    EFFECT_RECONCILED = "effect_reconciled"
    USER_WAIT = "user_wait"
    PROVIDER_FAILURE = "provider_failure"


class RunHealth(StrEnum):
    HEALTHY = "healthy"
    WAITING_LEGITIMATELY = "waiting_legitimately"
    DUPLICATE_LOOP = "duplicate_loop"
    COST_WITHOUT_PROGRESS = "cost_without_progress"
    PROVIDER_STALL = "provider_stall"
    DEPENDENCY_DEADLOCK = "dependency_deadlock"
    LEASE_LOST = "lease_lost"
    NEEDS_USER_INPUT = "needs_user_input"


class ProgressEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    occurred_at: datetime
    kind: ProgressEventKind
    invocation_hash: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.:-]{1,200}$")
    failure_class: FailureClass | None = None
    token_cost: int = Field(default=0, ge=0)


class ProgressWatchdogPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_duplicate_invocations: int = Field(default=2, ge=1, le=16)
    max_tokens_without_progress: int = Field(default=20_000, ge=1, le=10_000_000)
    max_consecutive_provider_failures: int = Field(default=3, ge=1, le=16)


class RunHealthFinding(BaseModel):
    """Observation only; it cannot cancel, retry, budget-expand, or mutate a Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    health: RunHealth
    failure_class: FailureClass | None
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=8)
    meaningful_progress_events: int = Field(ge=0)
    observed_token_cost: int = Field(ge=0)


class RunHealthViolation(RuntimeError):
    """Raised before another physical call would continue an unhealthy Run.

    The exception carries only a classified finding.  Raw prompts, provider
    payloads and source content must never cross this control boundary.
    """

    def __init__(self, finding: RunHealthFinding) -> None:
        self.finding = finding
        super().__init__(f"{finding.health.value}:{','.join(finding.reason_codes)}")


_MEANINGFUL = {
    ProgressEventKind.RESULT_ADMITTED,
    ProgressEventKind.COVERAGE_CHANGED,
    ProgressEventKind.CONFLICT_CHANGED,
    ProgressEventKind.TASK_UNLOCKED,
    ProgressEventKind.USER_DECISION_CONSUMED,
    ProgressEventKind.EFFECT_RECONCILED,
}


def evaluate_run_health(
    events: tuple[ProgressEvent, ...], *, policy: ProgressWatchdogPolicy | None = None
) -> RunHealthFinding:
    """Classify a bounded event window without treating activity as progress."""

    if not events:
        raise ValueError("Progress Watchdog requires at least one event")
    policy = policy or ProgressWatchdogPolicy()
    run_ids = {event.run_id for event in events}
    if len(run_ids) != 1:
        raise ValueError("Progress Watchdog window must belong to one AgentRun")
    ordered = tuple(sorted(events, key=lambda item: item.occurred_at))
    run_id = ordered[0].run_id
    progress = sum(event.kind in _MEANINGFUL for event in ordered)
    if ordered[-1].kind is ProgressEventKind.USER_WAIT:
        cost = sum(event.token_cost for event in ordered)
        return _finding(
            run_id,
            RunHealth.WAITING_LEGITIMATELY,
            FailureClass.USER_WAIT,
            "user_wait",
            progress,
            cost,
        )

    # A durable result admission (or another explicitly meaningful event)
    # starts a new no-progress window.  Earlier cost and retries remain in the
    # audit trail, but must not make a later healthy step look stuck; likewise,
    # one early success must not hide a subsequent loop forever.
    last_progress_index = max(
        (index for index, event in enumerate(ordered) if event.kind in _MEANINGFUL),
        default=-1,
    )
    window = ordered[last_progress_index + 1 :]
    cost = sum(event.token_cost for event in window)
    hashes = [event.invocation_hash for event in window if event.invocation_hash]
    if (
        window
        and hashes
        and max(hashes.count(value) for value in set(hashes)) > policy.max_duplicate_invocations
    ):
        return _finding(
            run_id,
            RunHealth.DUPLICATE_LOOP,
            FailureClass.NO_PROGRESS,
            "duplicate_invocation_without_progress",
            progress,
            cost,
        )
    if window and cost > policy.max_tokens_without_progress:
        return _finding(
            run_id,
            RunHealth.COST_WITHOUT_PROGRESS,
            FailureClass.NO_PROGRESS,
            "token_cost_without_progress",
            progress,
            cost,
        )
    failures = [event for event in window if event.kind is ProgressEventKind.PROVIDER_FAILURE]
    if window and len(failures) >= policy.max_consecutive_provider_failures:
        return _finding(
            run_id,
            RunHealth.PROVIDER_STALL,
            FailureClass.TRANSIENT,
            "provider_failures_without_progress",
            progress,
            cost,
        )
    return _finding(
        run_id, RunHealth.HEALTHY, None, "meaningful_progress_or_no_watchdog_breach", progress, cost
    )


def _finding(
    run_id: UUID,
    health: RunHealth,
    failure: FailureClass | None,
    reason: str,
    progress: int,
    cost: int,
) -> RunHealthFinding:
    return RunHealthFinding(
        run_id=run_id,
        health=health,
        failure_class=failure,
        reason_codes=(reason,),
        meaningful_progress_events=progress,
        observed_token_cost=cost,
    )


__all__ = [
    "FailureClass",
    "ProgressEvent",
    "ProgressEventKind",
    "ProgressWatchdogPolicy",
    "RunHealth",
    "RunHealthFinding",
    "RunHealthViolation",
    "evaluate_run_health",
]
