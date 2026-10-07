from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from aidison.domain.events import DomainEventMetadata, StoredDomainEvent
from aidison.evaluation.contracts import MetricStatus
from aidison.evaluation.metrics import check_replay_bundle_readiness
from aidison.evaluation.replay_bundle import replay_bundle_evaluation_case
from aidison.evaluation.runner import evaluate_case
from aidison.runtime.agent_run_events import AgentRunEventType, agent_run_lifecycle_metadata
from aidison.runtime.agent_runs import (
    AdmittedCheckpointRef,
    AgentRun,
    AgentRunKind,
    AgentRunStatus,
)
from aidison.runtime.contracts import (
    BudgetOperationKind,
    FailureClass,
    InvocationRecording,
    InvocationRecordingStatus,
)
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.runtime.replay_bundle import (
    ReplayBundleStatus,
    ReplayIncompleteError,
    build_evaluation_replay_bundle,
    verify_evaluation_replay_bundle,
)

PROJECT_ID = UUID("81000000-0000-4000-8000-000000000001")
RUN_ID = UUID("82000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
ARTIFACT_REF = f"artifact+sha256://{'a' * 64}/83000000-0000-4000-8000-000000000003"


def _binding() -> RuntimeBinding:
    return RuntimeBinding(
        runtime_family=RuntimeFamily.LANGGRAPH_V1,
        runtime_revision="runtime-v1",
        graph_key="research",
        graph_revision="research-v1",
        state_schema_version="research-state-v1",
        profile_binding_ref="profile://research/1",
        policy_binding_ref="policy://research/1",
    )


def _run(**updates: object) -> AgentRun:
    values: dict[str, object] = {
        "id": RUN_ID,
        "project_id": PROJECT_ID,
        "kind": AgentRunKind.RESEARCH,
        "idempotency_key": "replay-bundle-fixture",
        "basis_hash": sha256(b"replay bundle basis").hexdigest(),
        "basis_project_revision": 1,
        "runtime_binding": _binding(),
        "thread_id": "run-replay-bundle",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return AgentRun.model_validate(values)


def _event(
    *,
    run: AgentRun,
    event_type: AgentRunEventType,
    aggregate_version: int,
    project_seq: int,
) -> StoredDomainEvent:
    payload: dict[str, object] = {"run": run.model_dump(mode="json")}
    metadata: DomainEventMetadata = agent_run_lifecycle_metadata(
        run=run,
        aggregate_version=aggregate_version,
        payload=payload,
    )
    return StoredDomainEvent(
        **metadata.model_dump(),
        project_id=run.project_id,
        project_seq=project_seq,
        event_type=event_type.value,
        payload=payload,
        recorded_at=run.updated_at,
    )


def _event_backed_run() -> tuple[AgentRun, tuple[StoredDomainEvent, ...]]:
    queued = _run()
    running_at = NOW + timedelta(minutes=1)
    running = queued.model_copy(
        update={
            "status": AgentRunStatus.RUNNING,
            "current_generation": 1,
            "started_at": running_at,
            "updated_at": running_at,
        }
    )
    checkpoint_at = running_at + timedelta(minutes=1)
    checkpointed = running.model_copy(
        update={
            "admitted_checkpoint": AdmittedCheckpointRef(
                thread_id=running.thread_id,
                checkpoint_id="checkpoint-1",
                graph_revision=running.runtime_binding.graph_revision,
                state_schema_version=running.runtime_binding.state_schema_version,
                generation=1,
                event_cursor=3,
                invocation_recording_keys=("run:tool:1",),
            ),
            "updated_at": checkpoint_at,
        }
    )
    completed_at = checkpoint_at + timedelta(minutes=1)
    completed = checkpointed.model_copy(
        update={
            "status": AgentRunStatus.SUCCEEDED,
            "updated_at": completed_at,
            "completed_at": completed_at,
        }
    )
    return completed, (
        _event(
            run=queued,
            event_type=AgentRunEventType.RESEARCH_QUEUED,
            aggregate_version=1,
            project_seq=1,
        ),
        _event(
            run=running,
            event_type=AgentRunEventType.RUNNING,
            aggregate_version=2,
            project_seq=2,
        ),
        _event(
            run=checkpointed,
            event_type=AgentRunEventType.CHECKPOINT_ADMITTED,
            aggregate_version=3,
            project_seq=3,
        ),
        _event(
            run=completed,
            event_type=AgentRunEventType.SUCCEEDED,
            aggregate_version=4,
            project_seq=4,
        ),
    )


def _recording(run: AgentRun) -> InvocationRecording:
    return InvocationRecording(
        project_id=run.project_id,
        agent_run_id=run.id,
        producer_attempt_id=UUID("84000000-0000-4000-8000-000000000004"),
        basis_hash=run.basis_hash,
        idempotency_key="run:tool:1",
        request_hash=sha256(b"replay bundle request").hexdigest(),
        kind=BudgetOperationKind.TOOL,
        provider="fixture-provider",
        operation_name="fixture_lookup",
        status=InvocationRecordingStatus.SUCCEEDED,
        response_artifact_ref=ARTIFACT_REF,
    )


def test_bundle_rebuilds_control_history_without_a_provider_or_tool_call() -> None:
    run, events = _event_backed_run()
    recording = _recording(run)

    bundle = build_evaluation_replay_bundle(
        run=run,
        lifecycle_events=events,
        invocations=(recording,),
        available_artifact_refs=frozenset({ARTIFACT_REF}),
    )
    verify_evaluation_replay_bundle(
        bundle,
        run=run,
        lifecycle_events=events,
        invocations=(recording,),
        available_artifact_refs=frozenset({ARTIFACT_REF}),
    )

    assert bundle.status is ReplayBundleStatus.REPLAYABLE
    assert bundle.event_cursor == 4
    assert bundle.admitted_checkpoint is not None
    assert bundle.admitted_checkpoint.event_cursor == 3
    assert bundle.checkpoint_event_cursor == 3
    assert [item.project_seq for item in bundle.checkpoint_lifecycle_events] == [1, 2, 3]
    assert [item.project_seq for item in bundle.post_checkpoint_lifecycle_events] == [4]
    assert bundle.invocations[0].response_artifact_ref == ARTIFACT_REF


def test_bundle_is_explicitly_incomplete_when_recorded_output_is_missing() -> None:
    run, events = _event_backed_run()
    recording = _recording(run)
    bundle = build_evaluation_replay_bundle(
        run=run,
        lifecycle_events=events,
        invocations=(recording,),
        available_artifact_refs=frozenset(),
    )

    assert bundle.status is ReplayBundleStatus.REPLAY_INCOMPLETE
    assert bundle.reason_codes == ("response_artifact_missing",)
    with pytest.raises(ReplayIncompleteError, match="response_artifact_missing"):
        verify_evaluation_replay_bundle(
            bundle,
            run=run,
            lifecycle_events=events,
            invocations=(recording,),
            available_artifact_refs=frozenset(),
        )


def test_bundle_fails_closed_when_historical_checkpoint_does_not_match_run_binding() -> None:
    run, events = _event_backed_run()
    checkpoint = run.admitted_checkpoint
    assert checkpoint is not None
    mismatched_checkpoint = checkpoint.model_copy(
        update={"graph_revision": "unknown-graph-revision"}
    )
    checkpointed_run = AgentRun.model_validate(events[2].payload["run"]).model_copy(
        update={"admitted_checkpoint": mismatched_checkpoint}
    )
    mismatched_run = run.model_copy(
        update={"admitted_checkpoint": mismatched_checkpoint}
    )
    mismatched_events = (
        events[0],
        events[1],
        _event(
            run=checkpointed_run,
            event_type=AgentRunEventType.CHECKPOINT_ADMITTED,
            aggregate_version=3,
            project_seq=3,
        ),
        _event(
            run=mismatched_run,
            event_type=AgentRunEventType.SUCCEEDED,
            aggregate_version=4,
            project_seq=4,
        ),
    )

    bundle = build_evaluation_replay_bundle(
        run=mismatched_run,
        lifecycle_events=mismatched_events,
        invocations=(_recording(run),),
        available_artifact_refs=frozenset({ARTIFACT_REF}),
    )

    assert bundle.status is ReplayBundleStatus.REPLAY_INCOMPLETE
    assert bundle.reason_codes == ("checkpoint_runtime_binding_mismatch",)


def test_bundle_explains_corrupt_artifact_and_ambiguous_effect_without_retrying() -> None:
    run, events = _event_backed_run()
    corrupt_recording = _recording(run)
    ambiguous_recording = corrupt_recording.model_copy(
        update={
            "idempotency_key": "run:tool:2",
            "status": InvocationRecordingStatus.AMBIGUOUS,
            "response_artifact_ref": None,
            "failure_class": FailureClass.UNKNOWN_EFFECT,
        }
    )

    bundle = build_evaluation_replay_bundle(
        run=run,
        lifecycle_events=events,
        invocations=(corrupt_recording, ambiguous_recording),
        available_artifact_refs=frozenset(),
        corrupt_artifact_refs=frozenset({ARTIFACT_REF}),
    )

    assert bundle.status is ReplayBundleStatus.REPLAY_INCOMPLETE
    assert bundle.reason_codes == (
        "ambiguous_invocation_effect",
        "response_artifact_integrity_failed",
    )


@pytest.mark.asyncio
async def test_verified_bundle_becomes_an_offline_trace_review_case() -> None:
    run, events = _event_backed_run()
    bundle = build_evaluation_replay_bundle(
        run=run,
        lifecycle_events=events,
        invocations=(_recording(run),),
        available_artifact_refs=frozenset({ARTIFACT_REF}),
    )

    case = replay_bundle_evaluation_case(bundle)
    result = await evaluate_case(case)

    assert case.fixture_kind.value == "trace_review"
    assert result.metrics[0].status is MetricStatus.PASS
    assert result.metrics[0].payload["manifest_hash"] == bundle.manifest_hash


def test_incomplete_bundle_cannot_be_promoted_to_an_evaluation_pass() -> None:
    run, events = _event_backed_run()
    bundle = build_evaluation_replay_bundle(
        run=run,
        lifecycle_events=events,
        invocations=(_recording(run),),
        available_artifact_refs=frozenset(),
    )

    metric = check_replay_bundle_readiness(bundle=bundle)

    assert metric.status is MetricStatus.FAIL
    assert metric.payload["reason_codes"] == ["response_artifact_missing"]
