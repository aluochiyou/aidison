"""Controlled replay and deterministic fault injection for recorded invocations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from aidison.runtime.contracts import (
    FailureClass,
    FaultInjection,
    FaultInjectionMode,
    InvocationRecording,
    InvocationRecordingStatus,
)


class ReplayConflictError(RuntimeError):
    """The idempotency key was reused for a different request."""


class ReplayUnavailableError(RuntimeError):
    """A recorded operation cannot safely produce a replayable response."""


class PreDispatchInvocationError(RuntimeError):
    """The adapter failed before it dispatched an external operation."""

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(str(cause))


class InjectedInvocationFault(RuntimeError):
    def __init__(self, failure_class: FailureClass) -> None:
        self.failure_class = failure_class
        super().__init__(f"injected invocation fault: {failure_class.value}")


class InvocationRecordingStore(Protocol):
    async def get(self, idempotency_key: str) -> InvocationRecording | None: ...

    async def prepare(self, recording: InvocationRecording) -> bool: ...

    async def record(self, recording: InvocationRecording) -> InvocationRecording: ...

    async def discard_prepared(self, recording: InvocationRecording) -> None: ...


class ReplayOutcome:
    def __init__(self, recording: InvocationRecording, *, replayed: bool) -> None:
        self.recording = recording
        self.replayed = replayed


class ReplayController:
    """Run one artifact-producing effect at most once per idempotency key.

    The live callable returns only an immutable artifact reference.  That keeps
    secrets and arbitrary provider payloads out of the replay ledger while
    still allowing a later run to skip the provider/tool completely.
    """

    def __init__(self, store: InvocationRecordingStore) -> None:
        self._store = store

    async def execute(
        self,
        requested: InvocationRecording,
        *,
        invoke: Callable[[], Awaitable[str]],
        recover_pending: Callable[[InvocationRecording], Awaitable[str | None]] | None = None,
        fault: FaultInjection | None = None,
    ) -> ReplayOutcome:
        stored = await self._store.get(requested.idempotency_key)
        prepared_here = False
        if stored is None:
            prepared_here = await self._store.prepare(requested)
            if not prepared_here:
                stored = await self._store.get(requested.idempotency_key)
                if stored is None:
                    raise ReplayConflictError("invocation preparation did not persist")
        if stored is not None:
            if not _matches_request(stored, requested):
                raise ReplayConflictError("invocation idempotency key has different content")
            if stored.status is InvocationRecordingStatus.AMBIGUOUS:
                raise ReplayUnavailableError("recorded invocation has unknown external effect")
            if stored.status is InvocationRecordingStatus.PENDING:
                if recover_pending is None:
                    raise ReplayUnavailableError(
                        "recorded invocation is prepared with unknown effect"
                    )
                response_ref = await recover_pending(stored)
                if response_ref is None:
                    raise ReplayUnavailableError(
                        "recorded invocation cannot be deterministically recovered"
                    )
                completed = stored.model_copy(
                    update={
                        "status": InvocationRecordingStatus.SUCCEEDED,
                        "response_artifact_ref": response_ref,
                        "failure_class": None,
                    }
                )
                recorded = await self._store.record(completed)
                if recorded != completed:
                    raise ReplayConflictError("stored invocation differs from recovered invocation")
                return ReplayOutcome(recorded, replayed=True)
            return ReplayOutcome(stored, replayed=True)

        active_fault = fault or FaultInjection()
        if active_fault.mode is FaultInjectionMode.BEFORE_DISPATCH_TIMEOUT and _targets(
            active_fault, requested
        ):
            if prepared_here:
                await self._store.discard_prepared(requested)
            raise InjectedInvocationFault(FailureClass.TIMEOUT)

        try:
            response_ref = await invoke()
        except PreDispatchInvocationError as exc:
            if prepared_here:
                await self._store.discard_prepared(requested)
            raise exc.cause from None
        if active_fault.mode is FaultInjectionMode.AFTER_EFFECT_UNKNOWN and _targets(
            active_fault, requested
        ):
            ambiguous = requested.model_copy(
                update={
                    "status": InvocationRecordingStatus.AMBIGUOUS,
                    "response_artifact_ref": None,
                    "failure_class": FailureClass.UNKNOWN_EFFECT,
                }
            )
            await self._store.record(ambiguous)
            raise InjectedInvocationFault(FailureClass.UNKNOWN_EFFECT)

        completed = requested.model_copy(
            update={
                "status": InvocationRecordingStatus.SUCCEEDED,
                "response_artifact_ref": response_ref,
                "failure_class": None,
            }
        )
        recorded = await self._store.record(completed)
        if recorded != completed:
            raise ReplayConflictError("stored invocation differs from completed invocation")
        return ReplayOutcome(recorded, replayed=False)

    async def record_unknown_effect(self, requested: InvocationRecording) -> None:
        """Fence one adapter-confirmed post-dispatch failure.

        The controller cannot infer whether an arbitrary callable reached an
        external provider.  Only an adapter that has durably marked its budget
        operation dispatched may use this method.
        """
        ambiguous = requested.model_copy(
            update={
                "status": InvocationRecordingStatus.AMBIGUOUS,
                "response_artifact_ref": None,
                "failure_class": FailureClass.UNKNOWN_EFFECT,
            }
        )
        await self._store.record(ambiguous)


def _targets(fault: FaultInjection, recording: InvocationRecording) -> bool:
    return fault.idempotency_key is None or fault.idempotency_key == recording.idempotency_key


def _matches_request(stored: InvocationRecording, requested: InvocationRecording) -> bool:
    return (
        stored.project_id == requested.project_id
        and stored.agent_run_id == requested.agent_run_id
        and stored.basis_hash == requested.basis_hash
        and stored.idempotency_key == requested.idempotency_key
        and stored.request_hash == requested.request_hash
        and stored.kind is requested.kind
        and stored.provider == requested.provider
        and stored.operation_name == requested.operation_name
    )
