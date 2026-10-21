from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from aidison.domain.events import (
    EventIntegrityError,
    EventSequenceGap,
    ReplaySnapshotIntegrityError,
    StoredDomainEvent,
    canonical_payload_hash,
)
from aidison.runtime.agent_run_events import (
    AgentRunEventType,
    AgentRunReplaySnapshot,
    agent_run_event_id,
    agent_run_lifecycle_metadata,
    agent_run_replay_state_hash,
    queued_agent_run_metadata,
    replay_agent_run,
    replay_agent_run_from_snapshot,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

PROJECT_ID = UUID("71000000-0000-4000-8000-000000000001")
RUN_ID = UUID("72000000-0000-4000-8000-000000000002")
CREATED_AT = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)


def _binding(*, graph_key: str) -> RuntimeBinding:
    return RuntimeBinding(
        runtime_family=RuntimeFamily.LANGGRAPH_V1,
        runtime_revision="runtime-v1",
        graph_key=graph_key,
        graph_revision=f"{graph_key}-v1",
        state_schema_version=f"{graph_key}-state-v1",
        profile_binding_ref=f"profile://{graph_key}/1",
        policy_binding_ref=f"policy://{graph_key}/1",
    )


def _run(kind: AgentRunKind = AgentRunKind.RESEARCH) -> AgentRun:
    graph_key = kind.value
    return AgentRun(
        id=RUN_ID,
        project_id=PROJECT_ID,
        kind=kind,
        idempotency_key=f"{kind.value}-fixture",
        basis_hash=sha256(b"AgentRun event fixture").hexdigest(),
        basis_project_revision=3,
        runtime_binding=_binding(graph_key=graph_key),
        thread_id=f"agent-run:{RUN_ID}",
        run_contract_ref="artifact://run-contract",
        coverage_contract_ref="artifact://coverage-contract",
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _queued_event(
    *,
    kind: AgentRunKind = AgentRunKind.RESEARCH,
    event_type: AgentRunEventType | None = None,
) -> StoredDomainEvent:
    run = _run(kind)
    expected = {
        AgentRunKind.RESEARCH: AgentRunEventType.RESEARCH_QUEUED,
        AgentRunKind.SOLUTION: AgentRunEventType.SOLUTION_QUEUED,
        AgentRunKind.IMPACT: AgentRunEventType.IMPACT_QUEUED,
    }.get(kind, AgentRunEventType.RESEARCH_QUEUED)
    payload: dict[str, object] = {
        "run": run.model_dump(mode="json"),
        "basis_hash": run.basis_hash,
    }
    metadata = queued_agent_run_metadata(
        run=run,
        payload=payload,
        artifact_refs=("artifact://run-contract", "artifact://coverage-contract"),
    )
    return StoredDomainEvent(
        **metadata.model_dump(),
        project_id=PROJECT_ID,
        project_seq=9,
        event_type=(event_type or expected).value,
        payload=payload,
        recorded_at=CREATED_AT,
    )


def _lifecycle_event(
    *,
    run: AgentRun,
    event_type: AgentRunEventType,
    aggregate_version: int,
) -> StoredDomainEvent:
    payload: dict[str, object] = {"run": run.model_dump(mode="json")}
    metadata = agent_run_lifecycle_metadata(
        run=run,
        aggregate_version=aggregate_version,
        payload=payload,
        artifact_refs=("artifact://run-contract", "artifact://coverage-contract"),
    )
    return StoredDomainEvent(
        **metadata.model_dump(),
        project_id=PROJECT_ID,
        project_seq=9 + aggregate_version,
        event_type=event_type.value,
        payload=payload,
        recorded_at=run.updated_at,
    )


def _claimed(run: AgentRun, *, generation: int) -> AgentRun:
    now = run.updated_at + timedelta(minutes=1)
    return run.model_copy(
        update={
            "status": AgentRunStatus.RUNNING,
            "current_generation": generation,
            "started_at": run.started_at or now,
            "updated_at": now,
        }
    )


@pytest.mark.parametrize(
    ("kind", "event_type"),
    [
        (AgentRunKind.RESEARCH, AgentRunEventType.RESEARCH_QUEUED),
        (AgentRunKind.SOLUTION, AgentRunEventType.SOLUTION_QUEUED),
        (AgentRunKind.IMPACT, AgentRunEventType.IMPACT_QUEUED),
    ],
)
def test_replay_rebuilds_queued_agent_run(
    kind: AgentRunKind,
    event_type: AgentRunEventType,
) -> None:
    event = _queued_event(kind=kind, event_type=event_type)

    state = replay_agent_run([event])

    assert state.agent_run == _run(kind)
    assert state.aggregate_version == 1
    assert event.event_id == agent_run_event_id(RUN_ID, 1)


def test_replay_is_idempotent_for_identical_queued_event() -> None:
    event = _queued_event()

    state = replay_agent_run([event, event])

    assert state.agent_run == _run()
    assert state.seen_event_ids == frozenset({event.event_id})


def test_snapshot_tail_replay_matches_full_lifecycle_replay() -> None:
    queued = _queued_event()
    running = _claimed(_run(), generation=1)
    running_event = _lifecycle_event(
        run=running,
        event_type=AgentRunEventType.RUNNING,
        aggregate_version=2,
    )
    snapshot = AgentRunReplaySnapshot.from_state(
        replay_agent_run([queued]),
        project_seq=queued.project_seq,
        created_at=queued.recorded_at,
    )

    full = replay_agent_run([queued, running_event])
    accelerated = replay_agent_run_from_snapshot(snapshot, [running_event])

    assert accelerated == full
    assert agent_run_replay_state_hash(accelerated) == agent_run_replay_state_hash(full)


def test_snapshot_tail_replay_rejects_event_at_or_before_snapshot_cursor() -> None:
    queued = _queued_event()
    snapshot = AgentRunReplaySnapshot.from_state(
        replay_agent_run([queued]),
        project_seq=queued.project_seq,
        created_at=queued.recorded_at,
    )

    with pytest.raises(ReplaySnapshotIntegrityError, match="at or before its cursor"):
        replay_agent_run_from_snapshot(snapshot, [queued])


def test_replay_rejects_queued_event_type_that_disagrees_with_run_kind() -> None:
    event = _queued_event(
        kind=AgentRunKind.RESEARCH,
        event_type=AgentRunEventType.SOLUTION_QUEUED,
    )

    with pytest.raises(EventIntegrityError, match="does not match"):
        replay_agent_run([event])


def test_replay_rejects_started_run_inside_queued_event() -> None:
    event = _queued_event()
    run = _run().model_copy(
        update={
            "status": AgentRunStatus.RUNNING,
            "current_generation": 1,
            "started_at": CREATED_AT,
        }
    )
    payload = {**event.payload, "run": run.model_dump(mode="json")}
    tampered = event.model_copy(
        update={"payload": payload, "payload_hash": canonical_payload_hash(payload)}
    )

    with pytest.raises(EventIntegrityError, match="already-started"):
        replay_agent_run([tampered])


def test_replay_rejects_duplicate_event_id_with_changed_envelope() -> None:
    event = _queued_event()
    changed = event.model_copy(update={"project_seq": event.project_seq + 1})

    with pytest.raises(EventIntegrityError, match="duplicate event ID"):
        replay_agent_run([event, changed])


def test_replay_rebuilds_claim_wait_requeue_reclaim_and_completion() -> None:
    queued = _queued_event()
    first_claim = _claimed(_run(), generation=1)
    waiting = first_claim.model_copy(
        update={
            "status": AgentRunStatus.WAITING,
            "updated_at": first_claim.updated_at + timedelta(minutes=1),
        }
    )
    requeued = waiting.model_copy(
        update={
            "status": AgentRunStatus.QUEUED,
            "updated_at": waiting.updated_at + timedelta(minutes=1),
        }
    )
    second_claim = _claimed(requeued, generation=2)
    succeeded = second_claim.model_copy(
        update={
            "status": AgentRunStatus.SUCCEEDED,
            "updated_at": second_claim.updated_at + timedelta(minutes=1),
            "completed_at": second_claim.updated_at + timedelta(minutes=1),
        }
    )

    state = replay_agent_run(
        [
            queued,
            _lifecycle_event(
                run=first_claim,
                event_type=AgentRunEventType.RUNNING,
                aggregate_version=2,
            ),
            _lifecycle_event(
                run=waiting,
                event_type=AgentRunEventType.WAITING,
                aggregate_version=3,
            ),
            _lifecycle_event(
                run=requeued,
                event_type=AgentRunEventType.REQUEUED,
                aggregate_version=4,
            ),
            _lifecycle_event(
                run=second_claim,
                event_type=AgentRunEventType.RUNNING,
                aggregate_version=5,
            ),
            _lifecycle_event(
                run=succeeded,
                event_type=AgentRunEventType.SUCCEEDED,
                aggregate_version=6,
            ),
        ]
    )

    assert state.agent_run == succeeded
    assert state.aggregate_version == 6


def test_replay_rejects_terminal_event_without_a_terminal_status() -> None:
    queued = _queued_event()
    running = _claimed(_run(), generation=1)

    with pytest.raises(EventIntegrityError, match="invalid final state"):
        replay_agent_run(
            [
                queued,
                _lifecycle_event(
                    run=running,
                    event_type=AgentRunEventType.RUNNING,
                    aggregate_version=2,
                ),
                _lifecycle_event(
                    run=running,
                    event_type=AgentRunEventType.SUCCEEDED,
                    aggregate_version=3,
                ),
            ]
        )


def test_replay_rejects_lifecycle_version_gap() -> None:
    queued = _queued_event()
    running = _claimed(_run(), generation=1)

    with pytest.raises(EventSequenceGap, match="expected AgentRun version"):
        replay_agent_run(
            [
                queued,
                _lifecycle_event(
                    run=running,
                    event_type=AgentRunEventType.RUNNING,
                    aggregate_version=3,
                ),
            ]
        )
