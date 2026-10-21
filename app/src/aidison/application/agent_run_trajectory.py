"""Read-only, payload-free diagnostics for one durable AgentRun.

The projection joins control-plane lifecycle events, physical-call budget
operations and the replay ledger.  It deliberately does not expose prompts,
responses, request hashes, idempotency keys, provider request IDs, artifact
references or LangGraph checkpoint state.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.domain.events import StoredDomainEvent
from aidison.domain.models import FrozenModel
from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.agent_runs import AgentRunControl, AgentRunNotFoundError
from aidison.infrastructure.replay import InvocationRecordingRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_run_budget import (
    AgentRunBudgetOperation,
    AgentRunBudgetOperationKind,
    AgentRunBudgetState,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.contracts import (
    BudgetOperationKind,
    FailureClass,
    InvocationRecording,
    InvocationRecordingStatus,
)
from aidison.runtime.identity import RuntimeFamily

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
_FAILED_ATTEMPT_STATES = {
    AgentRunBudgetState.RELEASED,
    AgentRunBudgetState.AMBIGUOUS,
}


class AgentRunTrajectoryRuntime(FrozenModel):
    runtime_family: RuntimeFamily
    graph_key: str
    graph_revision: str
    state_schema_version: str


class AgentRunTrajectoryEvent(FrozenModel):
    """One lifecycle transition with its payload and Artifact refs removed."""

    project_sequence: int = Field(ge=1)
    aggregate_version: int = Field(ge=1)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,199}$")
    occurred_at: datetime
    recorded_at: datetime
    actor: str
    source_component: str
    artifact_count: int = Field(ge=0)


class AgentRunTrajectoryAttempt(FrozenModel):
    """One reserved provider/tool attempt without request or response content."""

    attempt_id: UUID
    generation: int = Field(ge=1)
    kind: AgentRunBudgetOperationKind
    logical_step: str
    physical_attempt_number: int = Field(ge=1)
    provider: str
    target: str
    state: AgentRunBudgetState
    dispatched: bool
    reserved_tokens: int = Field(ge=0)
    consumed_tokens: int = Field(ge=0)
    reserved_tool_calls: int = Field(ge=0)
    consumed_tool_calls: int = Field(ge=0)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    created_at: datetime
    settled_at: datetime | None
    elapsed_ms: int | None = Field(default=None, ge=0)


class AgentRunTrajectoryEffect(FrozenModel):
    """One ordered replay-ledger outcome without hashes or Artifact references."""

    sequence: int = Field(ge=1)
    kind: BudgetOperationKind
    provider: str
    operation_name: str
    status: InvocationRecordingStatus
    failure_class: FailureClass | None = None


class AgentRunFirstObservedFailure(FrozenModel):
    """Earliest recorded attempt error; explicitly not a root-cause claim."""

    logical_step: str
    physical_attempt_number: int = Field(ge=1)
    error_code: str
    observed_at: datetime


class AgentRunTrajectoryFailureAnalysis(FrozenModel):
    first_observed_failure: AgentRunFirstObservedFailure | None
    terminal_failure_recorded: bool
    terminal_failure_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{0,99}$",
    )
    self_recovered: bool
    recovered_logical_steps: tuple[str, ...]
    unresolved_unknown_effect_record_count: int = Field(ge=0)
    observed_failure_classes: tuple[FailureClass, ...]
    root_cause_inferred: Literal[False] = False


class AgentRunTrajectorySummary(FrozenModel):
    lifecycle_event_count: int = Field(ge=0)
    provider_attempt_count: int = Field(ge=0)
    dispatched_attempt_count: int = Field(ge=0)
    settled_attempt_count: int = Field(ge=0)
    failed_attempt_count: int = Field(ge=0)
    replay_effect_count: int = Field(ge=0)
    pending_effect_count: int = Field(ge=0)
    ambiguous_effect_count: int = Field(ge=0)
    consumed_tokens: int = Field(ge=0)
    consumed_tool_calls: int = Field(ge=0)


class AgentRunTrajectory(FrozenModel):
    schema_version: Literal["agent-run-trajectory.v1"] = "agent-run-trajectory.v1"
    project_id: UUID
    agent_run_id: UUID
    kind: AgentRunKind
    status: AgentRunStatus
    basis_project_revision: int = Field(ge=1)
    current_generation: int = Field(ge=0)
    runtime: AgentRunTrajectoryRuntime
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    summary: AgentRunTrajectorySummary
    failure_analysis: AgentRunTrajectoryFailureAnalysis
    lifecycle_events: tuple[AgentRunTrajectoryEvent, ...]
    provider_attempts: tuple[AgentRunTrajectoryAttempt, ...]
    replay_effects: tuple[AgentRunTrajectoryEffect, ...]


class AgentRunTrajectoryService:
    """Build a deterministic diagnostic view from existing PostgreSQL facts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
    ) -> AgentRunTrajectory:
        run = await AgentRunControl(self._session).get(agent_run_id)
        if run is None or run.project_id != project_id:
            raise AgentRunNotFoundError("AgentRun not found")
        events = tuple(
            await PostgresDomainStore(self._session).list_aggregate_events(
                project_id,
                aggregate_type="agent_run",
                aggregate_id=agent_run_id,
            )
        )
        operations = await AgentRunBudgetLedger(self._session).list_operations_for_run(
            agent_run_id
        )
        recordings = await InvocationRecordingRepository(self._session).list_for_run(
            project_id=project_id,
            agent_run_id=agent_run_id,
        )
        return build_agent_run_trajectory(
            run=run,
            events=events,
            operations=operations,
            recordings=recordings,
        )


def build_agent_run_trajectory(
    *,
    run: AgentRun,
    events: tuple[StoredDomainEvent, ...],
    operations: tuple[AgentRunBudgetOperation, ...],
    recordings: tuple[InvocationRecording, ...],
) -> AgentRunTrajectory:
    """Create a payload-free projection and conservative failure analysis."""

    if any(event.project_id != run.project_id or event.aggregate_id != run.id for event in events):
        raise ValueError("trajectory lifecycle events must belong to the AgentRun")
    if any(
        recording.project_id != run.project_id or recording.agent_run_id != run.id
        for recording in recordings
    ):
        raise ValueError("trajectory replay effects must belong to the AgentRun")

    ordered_operations = tuple(
        sorted(
            operations,
            key=lambda item: (
                item.created_at,
                item.claim_generation,
                item.logical_step,
                item.physical_attempt_no,
                item.id,
            ),
        )
    )
    event_views = tuple(_event_view(event) for event in events)
    attempt_views = tuple(_attempt_view(operation) for operation in ordered_operations)
    effect_views = tuple(
        AgentRunTrajectoryEffect(
            sequence=index,
            kind=recording.kind,
            provider=recording.provider,
            operation_name=recording.operation_name,
            status=recording.status,
            failure_class=recording.failure_class,
        )
        for index, recording in enumerate(recordings, start=1)
    )

    failed_operations = tuple(
        operation
        for operation in ordered_operations
        if operation.state in _FAILED_ATTEMPT_STATES and operation.normalized_error is not None
    )
    first_failure = None
    if failed_operations:
        first_operation = failed_operations[0]
        failure_code = first_operation.normalized_error
        if failure_code is None:  # pragma: no cover - filtered immediately above
            raise ValueError("failed trajectory attempt is missing its normalized error")
        first_failure = AgentRunFirstObservedFailure(
            logical_step=first_operation.logical_step,
            physical_attempt_number=first_operation.physical_attempt_no,
            error_code=_safe_code(failure_code),
            observed_at=first_operation.settled_at or first_operation.created_at,
        )
    recovered_steps = tuple(
        sorted(
            {
                failure.logical_step
                for failure in failed_operations
                if any(
                    later.logical_step == failure.logical_step
                    and later.state is AgentRunBudgetState.SETTLED
                    and (
                        later.claim_generation,
                        later.physical_attempt_no,
                    )
                    > (
                        failure.claim_generation,
                        failure.physical_attempt_no,
                    )
                    for later in ordered_operations
                )
            }
        )
    )
    terminal_failure_code = _terminal_failure_code(events)
    failure_classes = tuple(
        sorted(
            {
                recording.failure_class
                for recording in recordings
                if recording.failure_class is not None
            },
            key=lambda item: item.value,
        )
    )
    unknown_effect_count = sum(
        operation.state is AgentRunBudgetState.AMBIGUOUS for operation in ordered_operations
    ) + sum(recording.status is InvocationRecordingStatus.AMBIGUOUS for recording in recordings)

    binding = run.runtime_binding
    return AgentRunTrajectory(
        project_id=run.project_id,
        agent_run_id=run.id,
        kind=run.kind,
        status=run.status,
        basis_project_revision=run.basis_project_revision,
        current_generation=run.current_generation,
        runtime=AgentRunTrajectoryRuntime(
            runtime_family=binding.runtime_family,
            graph_key=binding.graph_key,
            graph_revision=binding.graph_revision,
            state_schema_version=binding.state_schema_version,
        ),
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        summary=AgentRunTrajectorySummary(
            lifecycle_event_count=len(event_views),
            provider_attempt_count=len(attempt_views),
            dispatched_attempt_count=sum(item.dispatched for item in attempt_views),
            settled_attempt_count=sum(
                item.state is AgentRunBudgetState.SETTLED for item in attempt_views
            ),
            failed_attempt_count=len(failed_operations),
            replay_effect_count=len(effect_views),
            pending_effect_count=sum(
                item.status is InvocationRecordingStatus.PENDING for item in effect_views
            ),
            ambiguous_effect_count=sum(
                item.status is InvocationRecordingStatus.AMBIGUOUS for item in effect_views
            ),
            consumed_tokens=sum(item.consumed_tokens for item in attempt_views),
            consumed_tool_calls=sum(item.consumed_tool_calls for item in attempt_views),
        ),
        failure_analysis=AgentRunTrajectoryFailureAnalysis(
            first_observed_failure=first_failure,
            terminal_failure_recorded=run.status is AgentRunStatus.FAILED,
            terminal_failure_code=terminal_failure_code,
            self_recovered=bool(recovered_steps),
            recovered_logical_steps=recovered_steps,
            unresolved_unknown_effect_record_count=unknown_effect_count,
            observed_failure_classes=failure_classes,
        ),
        lifecycle_events=event_views,
        provider_attempts=attempt_views,
        replay_effects=effect_views,
    )


def _event_view(event: StoredDomainEvent) -> AgentRunTrajectoryEvent:
    return AgentRunTrajectoryEvent(
        project_sequence=event.project_seq,
        aggregate_version=event.aggregate_version,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        actor=event.actor,
        source_component=event.source_component,
        artifact_count=len(event.artifact_refs),
    )


def _attempt_view(operation: AgentRunBudgetOperation) -> AgentRunTrajectoryAttempt:
    elapsed_ms = None
    if operation.settled_at is not None:
        elapsed_ms = max(
            0,
            int((operation.settled_at - operation.created_at).total_seconds() * 1000),
        )
    return AgentRunTrajectoryAttempt(
        attempt_id=operation.id,
        generation=operation.claim_generation,
        kind=operation.kind,
        logical_step=operation.logical_step,
        physical_attempt_number=operation.physical_attempt_no,
        provider=operation.provider,
        target=operation.target,
        state=operation.state,
        dispatched=operation.dispatched_at is not None,
        reserved_tokens=operation.reserved_tokens,
        consumed_tokens=operation.consumed_tokens,
        reserved_tool_calls=operation.reserved_tool_calls,
        consumed_tool_calls=operation.consumed_tool_calls,
        error_code=(
            None if operation.normalized_error is None else _safe_code(operation.normalized_error)
        ),
        created_at=operation.created_at,
        settled_at=operation.settled_at,
        elapsed_ms=elapsed_ms,
    )


def _safe_code(value: str) -> str:
    normalized = value.strip().lower()
    return normalized if _SAFE_CODE.fullmatch(normalized) else "unclassified"


def _terminal_failure_code(events: tuple[StoredDomainEvent, ...]) -> str | None:
    for event in reversed(events):
        if event.event_type != "agent_run.failed":
            continue
        code = event.payload.get("failure_code")
        if isinstance(code, str) and code.strip():
            return _safe_code(code)
    return None


__all__ = [
    "AgentRunTrajectory",
    "AgentRunTrajectoryService",
    "build_agent_run_trajectory",
]
