from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from aidison.domain.events import EventSequenceGap, ReplaySnapshotIntegrityError, StoredDomainEvent
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ResearchResultStatus,
    ResultEnvelope,
)
from aidison.runtime.agent_result_events import (
    AgentResultEventType,
    AgentResultReplaySnapshot,
    agent_result_event_metadata,
    agent_result_replay_state_hash,
    replay_agent_result,
    replay_agent_result_from_snapshot,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _stored_event(
    *,
    project_id: UUID,
    result: ResultEnvelope,
    version: int,
    event_type: AgentResultEventType,
    payload: dict[str, object],
    sequence: int,
) -> StoredDomainEvent:
    occurred_at = datetime(2026, 10, 6, tzinfo=UTC)
    metadata = agent_result_event_metadata(
        result=result,
        aggregate_version=version,
        payload=payload,
        occurred_at=occurred_at,
    )
    return StoredDomainEvent(
        **metadata.model_dump(),
        project_id=project_id,
        project_seq=sequence,
        event_type=event_type.value,
        payload=payload,
        recorded_at=occurred_at,
    )


def _result() -> ResultEnvelope:
    return ResultEnvelope(
        run_id=uuid4(),
        task_id=uuid4(),
        basis_hash=_hash("basis"),
        producer_attempt_id=uuid4(),
        producer_generation=1,
        producer_profile_ref="profile://research/1",
        status=ResearchResultStatus.SUCCEEDED,
        artifact_ref=f"artifact+sha256://{_hash('result')}/result.json",
        manifest_hash=_hash("manifest"),
        evidence_refs=(),
        coverage_observation_refs=(),
        unresolved_refs=(),
    )


def test_result_replay_preserves_untrusted_result_and_admission_as_separate_facts() -> None:
    project_id = uuid4()
    result = _result()
    admission = AdmissionRecord(
        run_id=result.run_id,
        result_id=result.id,
        result_manifest_hash=result.manifest_hash,
        disposition=AdmissionDisposition.ACCEPTED,
        reason_codes=("runtime_fenced", "schema_valid"),
        admitted_ref="admitted://result/1",
    )
    recorded = _stored_event(
        project_id=project_id,
        result=result,
        version=1,
        event_type=AgentResultEventType.RECORDED,
        payload={"result": result.model_dump(mode="json")},
        sequence=1,
    )
    accepted = _stored_event(
        project_id=project_id,
        result=result,
        version=2,
        event_type=AgentResultEventType.ACCEPTED,
        payload={
            "result": result.model_dump(mode="json"),
            "admission": admission.model_dump(mode="json"),
        },
        sequence=2,
    )

    state = replay_agent_result([recorded, accepted])

    assert state.result_envelope == result
    assert state.admission_record == admission
    assert state.aggregate_version == 2


def test_result_snapshot_tail_replay_preserves_the_separate_admission_verdict() -> None:
    project_id = uuid4()
    result = _result()
    admission = AdmissionRecord(
        run_id=result.run_id,
        result_id=result.id,
        result_manifest_hash=result.manifest_hash,
        disposition=AdmissionDisposition.ACCEPTED,
        reason_codes=("runtime_fenced", "schema_valid"),
        admitted_ref="admitted://result/1",
    )
    recorded = _stored_event(
        project_id=project_id,
        result=result,
        version=1,
        event_type=AgentResultEventType.RECORDED,
        payload={"result": result.model_dump(mode="json")},
        sequence=1,
    )
    accepted = _stored_event(
        project_id=project_id,
        result=result,
        version=2,
        event_type=AgentResultEventType.ACCEPTED,
        payload={
            "result": result.model_dump(mode="json"),
            "admission": admission.model_dump(mode="json"),
        },
        sequence=2,
    )
    snapshot = AgentResultReplaySnapshot.from_state(
        replay_agent_result([recorded]),
        project_seq=recorded.project_seq,
        created_at=recorded.recorded_at,
    )

    full = replay_agent_result([recorded, accepted])
    accelerated = replay_agent_result_from_snapshot(snapshot, [accepted])

    assert accelerated == full
    assert accelerated.admission_record == admission
    assert agent_result_replay_state_hash(accelerated) == agent_result_replay_state_hash(full)


def test_result_snapshot_tail_replay_rejects_an_event_at_or_before_its_cursor() -> None:
    project_id = uuid4()
    result = _result()
    recorded = _stored_event(
        project_id=project_id,
        result=result,
        version=1,
        event_type=AgentResultEventType.RECORDED,
        payload={"result": result.model_dump(mode="json")},
        sequence=1,
    )
    snapshot = AgentResultReplaySnapshot.from_state(
        replay_agent_result([recorded]),
        project_seq=recorded.project_seq,
        created_at=recorded.recorded_at,
    )

    with pytest.raises(ReplaySnapshotIntegrityError, match="at or before its cursor"):
        replay_agent_result_from_snapshot(snapshot, [recorded])


def test_result_replay_rejects_admission_without_the_recorded_result() -> None:
    project_id = uuid4()
    result = _result()
    admission = AdmissionRecord(
        run_id=result.run_id,
        result_id=result.id,
        result_manifest_hash=result.manifest_hash,
        disposition=AdmissionDisposition.REJECTED,
        reason_codes=("artifact_missing",),
    )
    rejected = _stored_event(
        project_id=project_id,
        result=result,
        version=2,
        event_type=AgentResultEventType.REJECTED,
        payload={
            "result": result.model_dump(mode="json"),
            "admission": admission.model_dump(mode="json"),
        },
        sequence=1,
    )

    with pytest.raises(EventSequenceGap, match="start at version 1"):
        replay_agent_result([rejected])
