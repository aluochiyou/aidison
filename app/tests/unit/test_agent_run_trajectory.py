from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from aidison.application.agent_run_trajectory import build_agent_run_trajectory
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
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

_NOW = datetime(2026, 10, 9, tzinfo=UTC)
_HASH = sha256(b"trajectory-fixture").hexdigest()


def _run(**changes: object) -> AgentRun:
    payload: dict[str, object] = {
        "id": UUID("00000000-0000-0000-0000-000000000101"),
        "project_id": UUID("00000000-0000-0000-0000-000000000102"),
        "kind": AgentRunKind.RESEARCH,
        "idempotency_key": "trajectory-run",
        "basis_hash": _HASH,
        "basis_project_revision": 3,
        "runtime_binding": RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="research",
            graph_revision="research-r7",
            state_schema_version="research-state-v2",
            profile_binding_ref="profile://research/default",
            policy_binding_ref="policy://research/default",
        ),
        "thread_id": "trajectory-thread",
        "status": AgentRunStatus.SUCCEEDED,
        "current_generation": 1,
        "created_at": _NOW,
        "started_at": _NOW,
        "updated_at": _NOW + timedelta(seconds=4),
        "completed_at": _NOW + timedelta(seconds=4),
    }
    payload.update(changes)
    return AgentRun.model_validate(payload)


def _operation(
    *,
    attempt_number: int,
    state: AgentRunBudgetState,
    created_offset: int,
    normalized_error: str | None = None,
    consumed_tokens: int = 0,
) -> AgentRunBudgetOperation:
    settled = _NOW + timedelta(seconds=created_offset + 1)
    return AgentRunBudgetOperation(
        id=uuid4(),
        account_id=UUID("00000000-0000-0000-0000-000000000103"),
        claim_generation=1,
        lease_token=UUID("00000000-0000-0000-0000-000000000104"),
        kind=AgentRunBudgetOperationKind.MODEL,
        logical_step="research.model",
        physical_attempt_no=attempt_number,
        idempotency_key=f"trajectory-attempt-{attempt_number}",
        request_hash=sha256(f"request-{attempt_number}".encode()).hexdigest(),
        provider="openai",
        target="gpt-test",
        state=state,
        reserved_tokens=50,
        consumed_tokens=consumed_tokens,
        normalized_error=normalized_error,
        created_at=_NOW + timedelta(seconds=created_offset),
        dispatched_at=(
            _NOW + timedelta(seconds=created_offset)
            if state is not AgentRunBudgetState.RELEASED
            else None
        ),
        settled_at=settled,
    )


def _recording(*, project_id: UUID | None = None) -> InvocationRecording:
    run = _run()
    return InvocationRecording(
        project_id=project_id or run.project_id,
        agent_run_id=run.id,
        producer_attempt_id=uuid4(),
        basis_hash=run.basis_hash,
        idempotency_key="trajectory-effect",
        request_hash=sha256(b"effect-request").hexdigest(),
        kind=BudgetOperationKind.TOOL,
        provider="web-search",
        operation_name="search",
        status=InvocationRecordingStatus.AMBIGUOUS,
        failure_class=FailureClass.UNKNOWN_EFFECT,
    )


def test_trajectory_distinguishes_first_failure_from_later_self_recovery() -> None:
    trajectory = build_agent_run_trajectory(
        run=_run(),
        events=(),
        operations=(
            _operation(
                attempt_number=1,
                state=AgentRunBudgetState.RELEASED,
                created_offset=1,
                normalized_error="quota_unavailable",
            ),
            _operation(
                attempt_number=2,
                state=AgentRunBudgetState.SETTLED,
                created_offset=2,
                consumed_tokens=31,
            ),
        ),
        recordings=(_recording(),),
    )

    assert trajectory.failure_analysis.first_observed_failure is not None
    assert trajectory.failure_analysis.first_observed_failure.error_code == "quota_unavailable"
    assert trajectory.failure_analysis.self_recovered is True
    assert trajectory.failure_analysis.recovered_logical_steps == ("research.model",)
    assert trajectory.failure_analysis.root_cause_inferred is False
    assert trajectory.failure_analysis.unresolved_unknown_effect_record_count == 1
    assert trajectory.summary.consumed_tokens == 31
    assert trajectory.summary.provider_attempt_count == 2
    assert trajectory.summary.dispatched_attempt_count == 1

    serialized = trajectory.model_dump(mode="json")
    rendered = str(serialized)
    for secret_field in (
        "request_hash",
        "idempotency_key",
        "provider_request_id",
        "response_artifact_ref",
        "profile_binding_ref",
        "policy_binding_ref",
    ):
        assert secret_field not in rendered


def test_trajectory_rejects_replay_effect_from_another_project() -> None:
    with pytest.raises(ValueError, match="replay effects must belong"):
        build_agent_run_trajectory(
            run=_run(),
            events=(),
            operations=(),
            recordings=(_recording(project_id=uuid4()),),
        )
