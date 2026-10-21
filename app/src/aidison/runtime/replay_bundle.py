"""Deterministic, side-effect-free evaluation bundles for event-backed AgentRuns.

The bundle deliberately references a LangGraph checkpoint instead of copying
GraphState.  It validates the control-plane history and recorded invocation
outputs, then allows evaluation code to prove that no live provider/tool call
is required to inspect that history.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from aidison.domain.events import StoredDomainEvent, canonical_payload_hash
from aidison.domain.models import FrozenModel
from aidison.runtime.agent_run_events import AgentRunEventType, replay_agent_run
from aidison.runtime.agent_runs import (
    AdmittedCheckpointRef,
    AgentRun,
    execution_checkpoint_thread_id,
)
from aidison.runtime.contracts import (
    FailureClass,
    InvocationRecording,
    InvocationRecordingStatus,
)
from aidison.runtime.identity import RuntimeBinding


class ReplayBundleIntegrityError(RuntimeError):
    """Events, run relation, or recording ownership cannot form one bundle."""


class ReplayIncompleteError(RuntimeError):
    """A replay bundle lacks an anchor or immutable output required for replay."""


class ReplayBundleStatus(StrEnum):
    REPLAYABLE = "replayable"
    REPLAY_INCOMPLETE = "replay_incomplete"


class LifecycleEventReference(FrozenModel):
    """Secret-free identity for one exact lifecycle event in a bundle."""

    event_id: UUID
    project_seq: int = Field(ge=1)
    aggregate_version: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=200)
    schema_version: int = Field(ge=1)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @classmethod
    def from_event(cls, event: StoredDomainEvent) -> LifecycleEventReference:
        return cls(
            event_id=event.event_id,
            project_seq=event.project_seq,
            aggregate_version=event.aggregate_version,
            event_type=event.event_type,
            schema_version=event.schema_version,
            payload_hash=event.payload_hash,
        )


class InvocationReplayReference(FrozenModel):
    """Replay-safe invocation identity; never contains provider payloads or secrets."""

    idempotency_key: str = Field(min_length=1, max_length=300)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: InvocationRecordingStatus
    response_artifact_ref: str | None = Field(default=None, max_length=500)
    failure_class: FailureClass | None = None

    @classmethod
    def from_recording(cls, recording: InvocationRecording) -> InvocationReplayReference:
        return cls(
            idempotency_key=recording.idempotency_key,
            request_hash=recording.request_hash,
            status=recording.status,
            response_artifact_ref=recording.response_artifact_ref,
            failure_class=recording.failure_class,
        )


class EvaluationReplayBundle(FrozenModel):
    """A frozen, read-only contract for deterministic AgentRun evaluation."""

    project_id: UUID
    agent_run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_binding: RuntimeBinding
    admitted_checkpoint: AdmittedCheckpointRef | None = None
    event_cursor: int = Field(ge=0)
    checkpoint_event_cursor: int = Field(ge=0)
    checkpoint_lifecycle_events: tuple[LifecycleEventReference, ...] = Field(
        default=(), max_length=512
    )
    post_checkpoint_lifecycle_events: tuple[LifecycleEventReference, ...] = Field(
        default=(), max_length=512
    )
    invocations: tuple[InvocationReplayReference, ...] = Field(default=(), max_length=512)
    status: ReplayBundleStatus
    reason_codes: tuple[str, ...] = Field(default=(), max_length=32)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def status_and_reasons_are_consistent(self) -> EvaluationReplayBundle:
        if self.status is ReplayBundleStatus.REPLAYABLE and self.reason_codes:
            raise ValueError("replayable bundle cannot have incompleteness reasons")
        if self.status is ReplayBundleStatus.REPLAY_INCOMPLETE and not self.reason_codes:
            raise ValueError("incomplete bundle requires explicit reason codes")
        if len({item.idempotency_key for item in self.invocations}) != len(self.invocations):
            raise ValueError("replay bundle invocation keys must be unique")
        lifecycle_events = self.lifecycle_events
        if not lifecycle_events:
            raise ValueError("replay bundle requires lifecycle events")
        if self.event_cursor != max(item.project_seq for item in lifecycle_events):
            raise ValueError("replay bundle event cursor must match its latest lifecycle event")
        if self.admitted_checkpoint is None and self.checkpoint_event_cursor != 0:
            raise ValueError("bundle without an admitted checkpoint cannot have checkpoint cursor")
        if (
            self.admitted_checkpoint is not None
            and self.checkpoint_event_cursor != self.admitted_checkpoint.event_cursor
        ):
            raise ValueError("bundle checkpoint cursor must match admitted checkpoint")
        if any(
            item.project_seq > self.checkpoint_event_cursor
            for item in self.checkpoint_lifecycle_events
        ):
            raise ValueError("checkpoint lifecycle event exceeds checkpoint cursor")
        if any(
            item.project_seq <= self.checkpoint_event_cursor
            for item in self.post_checkpoint_lifecycle_events
        ):
            raise ValueError("post-checkpoint event does not follow checkpoint cursor")
        if canonical_payload_hash(_manifest_payload(self)) != self.manifest_hash:
            raise ValueError("replay bundle manifest hash mismatch")
        return self

    @property
    def lifecycle_events(self) -> tuple[LifecycleEventReference, ...]:
        """Whole ordered AgentRun stream, split at the checkpoint boundary."""
        return self.checkpoint_lifecycle_events + self.post_checkpoint_lifecycle_events

    def require_replayable(self) -> None:
        if self.status is not ReplayBundleStatus.REPLAYABLE:
            raise ReplayIncompleteError(
                "replay bundle is incomplete: " + ", ".join(self.reason_codes)
            )


def build_evaluation_replay_bundle(
    *,
    run: AgentRun,
    lifecycle_events: tuple[StoredDomainEvent, ...],
    invocations: tuple[InvocationRecording, ...],
    available_artifact_refs: frozenset[str],
    corrupt_artifact_refs: frozenset[str] = frozenset(),
) -> EvaluationReplayBundle:
    """Build a read-only bundle without calling a provider, tool, or graph.

    Any corrupt control-plane history raises an integrity error.  Missing
    checkpoint metadata or immutable outputs instead produces the explicit
    ``replay_incomplete`` state required by the E3 contract.
    """
    if not lifecycle_events:
        raise ReplayBundleIntegrityError("AgentRun replay bundle requires lifecycle events")
    replayed = replay_agent_run(list(lifecycle_events))
    if replayed.agent_run != run:
        raise ReplayBundleIntegrityError(
            "AgentRun relation differs from deterministic lifecycle replay"
        )
    if any(
        event.project_id != run.project_id or event.aggregate_id != run.id
        for event in lifecycle_events
    ):
        raise ReplayBundleIntegrityError("lifecycle event belongs to another AgentRun")
    if any(
        recording.project_id != run.project_id
        or recording.agent_run_id != run.id
        or recording.basis_hash != run.basis_hash
        for recording in invocations
    ):
        raise ReplayBundleIntegrityError("invocation recording does not belong to AgentRun")

    sorted_events = tuple(sorted(lifecycle_events, key=lambda item: item.project_seq))
    sorted_invocations = tuple(sorted(invocations, key=lambda item: item.idempotency_key))
    reasons = _incompleteness_reasons(
        run=run,
        lifecycle_events=sorted_events,
        invocations=sorted_invocations,
        available_artifact_refs=available_artifact_refs,
        corrupt_artifact_refs=corrupt_artifact_refs,
    )
    status = (
        ReplayBundleStatus.REPLAYABLE
        if not reasons
        else ReplayBundleStatus.REPLAY_INCOMPLETE
    )
    event_refs = tuple(LifecycleEventReference.from_event(event) for event in sorted_events)
    checkpoint_cursor = run.admitted_checkpoint.event_cursor if run.admitted_checkpoint else 0
    checkpoint_event_refs = tuple(
        reference
        for reference in event_refs
        if checkpoint_cursor > 0 and reference.project_seq <= checkpoint_cursor
    )
    post_checkpoint_event_refs = tuple(
        reference
        for reference in event_refs
        if reference.project_seq > checkpoint_cursor
    )
    invocation_refs = tuple(
        InvocationReplayReference.from_recording(recording)
        for recording in sorted_invocations
    )
    event_cursor = max(event.project_seq for event in sorted_events)
    draft = EvaluationReplayBundle.model_construct(
        project_id=run.project_id,
        agent_run_id=run.id,
        basis_hash=run.basis_hash,
        runtime_binding=run.runtime_binding,
        admitted_checkpoint=run.admitted_checkpoint,
        event_cursor=event_cursor,
        checkpoint_event_cursor=checkpoint_cursor,
        checkpoint_lifecycle_events=checkpoint_event_refs,
        post_checkpoint_lifecycle_events=post_checkpoint_event_refs,
        invocations=invocation_refs,
        status=status,
        reason_codes=tuple(reasons),
        manifest_hash="0" * 64,
    )
    return EvaluationReplayBundle(
        project_id=run.project_id,
        agent_run_id=run.id,
        basis_hash=run.basis_hash,
        runtime_binding=run.runtime_binding,
        admitted_checkpoint=run.admitted_checkpoint,
        event_cursor=event_cursor,
        checkpoint_event_cursor=checkpoint_cursor,
        checkpoint_lifecycle_events=checkpoint_event_refs,
        post_checkpoint_lifecycle_events=post_checkpoint_event_refs,
        invocations=invocation_refs,
        status=status,
        reason_codes=tuple(reasons),
        manifest_hash=canonical_payload_hash(_manifest_payload(draft)),
    )


def verify_evaluation_replay_bundle(
    bundle: EvaluationReplayBundle,
    *,
    run: AgentRun,
    lifecycle_events: tuple[StoredDomainEvent, ...],
    invocations: tuple[InvocationRecording, ...],
    available_artifact_refs: frozenset[str],
    corrupt_artifact_refs: frozenset[str] = frozenset(),
) -> None:
    """Rebuild and compare a bundle without dispatching any external operation."""
    rebuilt = build_evaluation_replay_bundle(
        run=run,
        lifecycle_events=lifecycle_events,
        invocations=invocations,
        available_artifact_refs=available_artifact_refs,
        corrupt_artifact_refs=corrupt_artifact_refs,
    )
    if rebuilt != bundle:
        raise ReplayBundleIntegrityError("replay bundle no longer matches recorded inputs")
    bundle.require_replayable()


def _incompleteness_reasons(
    *,
    run: AgentRun,
    lifecycle_events: tuple[StoredDomainEvent, ...],
    invocations: tuple[InvocationRecording, ...],
    available_artifact_refs: frozenset[str],
    corrupt_artifact_refs: frozenset[str],
) -> list[str]:
    reasons: list[str] = []
    checkpoint = run.admitted_checkpoint
    if checkpoint is None:
        reasons.append("missing_admitted_checkpoint")
        return reasons
    if checkpoint.thread_id not in {
        run.thread_id,
        execution_checkpoint_thread_id(
            logical_thread_id=run.thread_id,
            generation=checkpoint.generation,
        ),
    }:
        reasons.append("checkpoint_thread_mismatch")
    if (
        checkpoint.graph_revision != run.runtime_binding.graph_revision
        or checkpoint.state_schema_version != run.runtime_binding.state_schema_version
    ):
        reasons.append("checkpoint_runtime_binding_mismatch")
    if checkpoint.event_cursor == 0:
        reasons.append("checkpoint_missing_event_cursor")
    checkpoint_event = next(
        (
            event
            for event in lifecycle_events
            if event.project_seq == checkpoint.event_cursor
        ),
        None,
    )
    if (
        checkpoint.event_cursor > 0
        and (
            checkpoint_event is None
            or checkpoint_event.event_type != AgentRunEventType.CHECKPOINT_ADMITTED.value
        )
    ):
        reasons.append("checkpoint_event_missing_or_mismatched")

    recordings_by_key = {recording.idempotency_key: recording for recording in invocations}
    for key in checkpoint.invocation_recording_keys:
        if key not in recordings_by_key:
            reasons.append("checkpoint_invocation_recording_missing")
            break
    for recording in invocations:
        if recording.status is not InvocationRecordingStatus.SUCCEEDED:
            if recording.status is InvocationRecordingStatus.AMBIGUOUS:
                reasons.append("ambiguous_invocation_effect")
            else:
                reasons.append("pending_invocation_effect")
            continue
        reference = recording.response_artifact_ref
        if reference is None:
            reasons.append("response_artifact_missing")
        elif reference in corrupt_artifact_refs:
            reasons.append("response_artifact_integrity_failed")
        elif reference not in available_artifact_refs:
            reasons.append("response_artifact_missing")
    return sorted(set(reasons))


def _manifest_payload(bundle: EvaluationReplayBundle) -> dict[str, object]:
    """Normalize UUIDs, enums and models before content-addressing a bundle."""
    return bundle.model_dump(mode="json", exclude={"manifest_hash"})


__all__ = [
    "EvaluationReplayBundle",
    "InvocationReplayReference",
    "LifecycleEventReference",
    "ReplayBundleIntegrityError",
    "ReplayBundleStatus",
    "ReplayIncompleteError",
    "build_evaluation_replay_bundle",
    "verify_evaluation_replay_bundle",
]
