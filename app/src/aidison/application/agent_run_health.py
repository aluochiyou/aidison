"""Project durable Run activity into a pre-dispatch no-progress gate."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.database import session_scope
from aidison.infrastructure.orm import AgentRunResultAdmissionRow
from aidison.providers.model_gateway import ModelInvocationRequest, ModelTarget
from aidison.research.langgraph_contracts import AdmissionDisposition
from aidison.runtime.agent_run_budget import AgentRunBudgetOperation, AgentRunBudgetState
from aidison.runtime.run_health import (
    FailureClass,
    ProgressEvent,
    ProgressEventKind,
    ProgressWatchdogPolicy,
    RunHealth,
    RunHealthFinding,
    RunHealthViolation,
    evaluate_run_health,
)

_PROVIDER_AVAILABILITY_FAILURES = {
    "rate_limited",
    "transient_upstream",
    "connection_pre_dispatch",
    "timeout_after_dispatch",
    "malformed_response",
}

_DISPATCHED_STATES = {
    AgentRunBudgetState.DISPATCHED,
    AgentRunBudgetState.SETTLED,
    AgentRunBudgetState.AMBIGUOUS,
}


class AgentRunProgressWatchdog:
    """Reject another model dispatch when durable evidence shows no progress.

    This is deliberately a read-only guard.  It does not cancel a Run, retry a
    provider, increase a budget or write project facts.  The owning executor
    records the classified terminal transition under its live Run claim.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        policy: ProgressWatchdogPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._policy = policy or ProgressWatchdogPolicy()

    async def authorize(
        self,
        *,
        request: ModelInvocationRequest,
        target: ModelTarget,
        ordinal: int,
    ) -> None:
        """Evaluate the current physical attempt before reserving or dispatching it."""

        del ordinal  # An attempt number is audit metadata, not invocation identity.
        async with session_scope(self._session_factory) as session:
            operations = await AgentRunBudgetLedger(session).list_operations_for_run(
                request.run_id
            )
            admission_rows = (
                await session.scalars(
                    select(AgentRunResultAdmissionRow)
                    .where(
                        AgentRunResultAdmissionRow.agent_run_id == request.run_id,
                        AgentRunResultAdmissionRow.disposition
                        == AdmissionDisposition.ACCEPTED.value,
                    )
                    .order_by(
                        AgentRunResultAdmissionRow.created_at,
                        AgentRunResultAdmissionRow.id,
                    )
                )
            ).all()

        events = _progress_events(
            run_id=request.run_id,
            operations=operations,
            admitted_at=tuple(row.created_at for row in admission_rows),
        )
        candidate = ProgressEvent(
            run_id=request.run_id,
            occurred_at=datetime.now(UTC),
            kind=ProgressEventKind.INVOCATION_DISPATCHED,
            invocation_hash=_request_fingerprint(request=request, target=target),
        )
        finding = evaluate_run_health((*events, candidate), policy=self._policy)
        if finding.health is not RunHealth.HEALTHY:
            raise RunHealthViolation(finding)


def _progress_events(
    *,
    run_id: UUID,
    operations: tuple[AgentRunBudgetOperation, ...],
    admitted_at: tuple[datetime, ...],
) -> tuple[ProgressEvent, ...]:
    """Build a payload-free event window from PostgreSQL control records."""

    events: list[ProgressEvent] = []
    for operation in operations:
        if operation.state not in _DISPATCHED_STATES:
            continue
        occurred_at = operation.dispatched_at or operation.created_at
        token_cost = (
            operation.consumed_tokens
            if operation.state is AgentRunBudgetState.SETTLED
            else operation.reserved_tokens
        )
        events.append(
            ProgressEvent(
                run_id=run_id,
                occurred_at=occurred_at,
                kind=ProgressEventKind.INVOCATION_DISPATCHED,
                invocation_hash=_operation_fingerprint(operation),
                token_cost=token_cost,
            )
        )
        if operation.normalized_error in _PROVIDER_AVAILABILITY_FAILURES:
            events.append(
                ProgressEvent(
                    run_id=run_id,
                    occurred_at=operation.settled_at or occurred_at,
                    kind=ProgressEventKind.PROVIDER_FAILURE,
                    failure_class=FailureClass.TRANSIENT,
                )
            )
    events.extend(
        ProgressEvent(
            run_id=run_id,
            occurred_at=occurred_at,
            kind=ProgressEventKind.RESULT_ADMITTED,
        )
        for occurred_at in admitted_at
    )
    return tuple(sorted(events, key=lambda item: item.occurred_at))


def _request_fingerprint(*, request: ModelInvocationRequest, target: ModelTarget) -> str:
    logical_step = (
        request.budget_context.logical_step
        if request.budget_context is not None
        else f"model-task.{request.task_id}"
    )
    request_hash = (
        request.budget_context.request_hash
        if request.budget_context is not None
        else sha256(request.prompt_ref.encode("utf-8")).hexdigest()
    )
    return _fingerprint(
        logical_step,
        request_hash,
        f"{target.provider}:{target.model}@{target.revision}",
    )


def _operation_fingerprint(operation: AgentRunBudgetOperation) -> str:
    return _fingerprint(
        operation.logical_step,
        operation.request_hash,
        f"{operation.provider}:{operation.target}",
    )


def _fingerprint(logical_step: str, request_hash: str, target: str) -> str:
    return sha256(f"{logical_step}\0{request_hash}\0{target}".encode()).hexdigest()


def run_health_failure_code(finding: RunHealthFinding) -> str:
    """Map a classified finding to a stable user-safe executor failure code."""

    return {
        RunHealth.DUPLICATE_LOOP: "runtime_duplicate_invocation_loop",
        RunHealth.COST_WITHOUT_PROGRESS: "runtime_cost_without_progress",
        RunHealth.PROVIDER_STALL: "runtime_provider_stall",
    }.get(finding.health, "runtime_no_progress")


__all__ = ["AgentRunProgressWatchdog", "run_health_failure_code"]
