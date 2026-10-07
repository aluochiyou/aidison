from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.runtime.contracts import (
    BudgetOperationKind,
    FailureClass,
    FaultInjection,
    FaultInjectionMode,
    InvocationRecording,
    InvocationRecordingStatus,
)
from aidison.runtime.replay import (
    InjectedInvocationFault,
    PreDispatchInvocationError,
    ReplayConflictError,
    ReplayController,
    ReplayUnavailableError,
)


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, InvocationRecording] = {}

    async def get(self, idempotency_key: str) -> InvocationRecording | None:
        return self.items.get(idempotency_key)

    async def prepare(self, recording: InvocationRecording) -> bool:
        existing = self.items.get(recording.idempotency_key)
        if existing is not None:
            return False
        self.items[recording.idempotency_key] = recording
        return True

    async def record(self, recording: InvocationRecording) -> InvocationRecording:
        existing = self.items.get(recording.idempotency_key)
        if existing is None:
            raise ReplayConflictError("invocation recording was not prepared")
        if existing.status is InvocationRecordingStatus.PENDING:
            self.items[recording.idempotency_key] = recording
            return recording
        if existing != recording:
            raise ReplayConflictError("invocation recording replay conflicts with stored content")
        return existing

    async def discard_prepared(self, recording: InvocationRecording) -> None:
        existing = self.items.get(recording.idempotency_key)
        if existing == recording:
            del self.items[recording.idempotency_key]


def _requested(**changes: object) -> InvocationRecording:
    payload: dict[str, object] = {
        "project_id": uuid4(),
        "agent_run_id": uuid4(),
        "producer_attempt_id": uuid4(),
        "basis_hash": sha256(b"basis").hexdigest(),
        "idempotency_key": "attempt-1:github:1:abc",
        "request_hash": sha256(b"request").hexdigest(),
        "kind": BudgetOperationKind.TOOL,
        "provider": "github",
        "operation_name": "search_code",
        "status": InvocationRecordingStatus.PENDING,
    }
    payload.update(changes)
    return InvocationRecording.model_validate(payload)


@pytest.mark.asyncio
async def test_first_execution_records_artifact_then_replays_without_provider_call() -> None:
    calls = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "a" * 64 + "/" + str(uuid4())

    controller = ReplayController(MemoryStore())
    requested = _requested()
    first = await controller.execute(requested, invoke=invoke)
    second = await controller.execute(requested, invoke=invoke)

    assert first.replayed is False
    assert second.replayed is True
    assert calls == 1
    assert first.recording.response_artifact_ref == second.recording.response_artifact_ref


@pytest.mark.asyncio
async def test_replay_rejects_same_key_with_different_request() -> None:
    store = MemoryStore()
    controller = ReplayController(store)

    async def invoke() -> str:
        return "artifact+sha256://" + "a" * 64 + "/" + str(uuid4())

    requested = _requested()
    await controller.execute(requested, invoke=invoke)
    with pytest.raises(ReplayConflictError, match="different content"):
        await controller.execute(
            requested.model_copy(update={"request_hash": sha256(b"other request").hexdigest()}),
            invoke=invoke,
        )


@pytest.mark.asyncio
async def test_pre_dispatch_timeout_does_not_call_provider_or_create_record() -> None:
    calls = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "a" * 64 + "/" + str(uuid4())

    store = MemoryStore()
    with pytest.raises(InjectedInvocationFault) as error:
        await ReplayController(store).execute(
            _requested(),
            invoke=invoke,
            fault=FaultInjection(mode=FaultInjectionMode.BEFORE_DISPATCH_TIMEOUT),
        )

    assert error.value.failure_class is FailureClass.TIMEOUT
    assert calls == 0
    assert store.items == {}


@pytest.mark.asyncio
async def test_after_effect_fault_records_ambiguous_and_blocks_repeat_call() -> None:
    calls = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "a" * 64 + "/" + str(uuid4())

    store = MemoryStore()
    controller = ReplayController(store)
    requested = _requested()
    with pytest.raises(InjectedInvocationFault) as error:
        await controller.execute(
            requested,
            invoke=invoke,
            fault=FaultInjection(mode=FaultInjectionMode.AFTER_EFFECT_UNKNOWN),
        )
    assert error.value.failure_class is FailureClass.UNKNOWN_EFFECT
    assert calls == 1
    assert next(iter(store.items.values())).status is InvocationRecordingStatus.AMBIGUOUS

    with pytest.raises(ReplayUnavailableError, match="unknown external effect"):
        await controller.execute(requested, invoke=invoke)
    assert calls == 1


@pytest.mark.asyncio
async def test_pre_dispatch_adapter_failure_discards_prepare_and_allows_retry() -> None:
    calls = 0

    async def before_dispatch() -> str:
        raise PreDispatchInvocationError(RuntimeError("local operation setup failed"))

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "b" * 64 + "/" + str(uuid4())

    store = MemoryStore()
    controller = ReplayController(store)
    with pytest.raises(RuntimeError, match="local operation setup failed"):
        await controller.execute(_requested(), invoke=before_dispatch)
    assert store.items == {}

    outcome = await controller.execute(_requested(), invoke=invoke)
    assert outcome.replayed is False
    assert calls == 1


@pytest.mark.asyncio
async def test_existing_pending_record_blocks_provider_call() -> None:
    calls = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "c" * 64 + "/" + str(uuid4())

    requested = _requested()
    store = MemoryStore()
    await store.prepare(requested)
    with pytest.raises(ReplayUnavailableError, match="prepared with unknown effect"):
        await ReplayController(store).execute(requested, invoke=invoke)
    assert calls == 0


@pytest.mark.asyncio
async def test_pending_record_can_be_deterministically_recovered_without_provider_call() -> None:
    calls = 0
    recoveries = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "c" * 64 + "/" + str(uuid4())

    async def recover_pending(recording: InvocationRecording) -> str | None:
        nonlocal recoveries
        recoveries += 1
        assert recording.status is InvocationRecordingStatus.PENDING
        return "artifact+sha256://" + "e" * 64 + "/" + str(uuid4())

    requested = _requested()
    store = MemoryStore()
    await store.prepare(requested)

    outcome = await ReplayController(store).execute(
        requested,
        invoke=invoke,
        recover_pending=recover_pending,
    )

    assert outcome.replayed is True
    assert outcome.recording.status is InvocationRecordingStatus.SUCCEEDED
    assert calls == 0
    assert recoveries == 1


@pytest.mark.asyncio
async def test_pending_recovery_that_cannot_prove_a_result_stays_fail_closed() -> None:
    calls = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "c" * 64 + "/" + str(uuid4())

    async def recover_pending(_recording: InvocationRecording) -> str | None:
        return None

    requested = _requested()
    store = MemoryStore()
    await store.prepare(requested)
    with pytest.raises(ReplayUnavailableError, match="cannot be deterministically recovered"):
        await ReplayController(store).execute(
            requested,
            invoke=invoke,
            recover_pending=recover_pending,
        )
    assert calls == 0
    assert store.items[requested.idempotency_key].status is InvocationRecordingStatus.PENDING


@pytest.mark.asyncio
async def test_replay_survives_reclaim_with_new_producer_attempt_id() -> None:
    calls = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "d" * 64 + "/" + str(uuid4())

    first_attempt = _requested(idempotency_key="run-1:github:search_code:stable")
    reclaimed_attempt = first_attempt.model_copy(update={"producer_attempt_id": uuid4()})
    controller = ReplayController(MemoryStore())

    await controller.execute(first_attempt, invoke=invoke)
    outcome = await controller.execute(reclaimed_attempt, invoke=invoke)

    assert outcome.replayed is True
    assert calls == 1
    assert outcome.recording.producer_attempt_id == first_attempt.producer_attempt_id


@pytest.mark.asyncio
async def test_targeted_fault_does_not_affect_another_invocation() -> None:
    calls = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return "artifact+sha256://" + "a" * 64 + "/" + str(uuid4())

    first = _requested(idempotency_key="attempt-1:github:1:targeted")
    other = _requested(idempotency_key="attempt-1:github:2:unaffected")
    controller = ReplayController(MemoryStore())
    fault = FaultInjection(
        mode=FaultInjectionMode.BEFORE_DISPATCH_TIMEOUT,
        idempotency_key=first.idempotency_key,
    )
    with pytest.raises(InjectedInvocationFault):
        await controller.execute(first, invoke=invoke, fault=fault)

    outcome = await controller.execute(other, invoke=invoke, fault=fault)
    assert outcome.replayed is False
    assert outcome.recording.status is InvocationRecordingStatus.SUCCEEDED
    assert calls == 1


def test_recording_requires_artifact_on_success_and_unknown_effect_on_ambiguity() -> None:
    with pytest.raises(ValueError, match="response artifact"):
        _requested(status=InvocationRecordingStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="unknown_effect"):
        _requested(
            status=InvocationRecordingStatus.AMBIGUOUS,
            failure_class=FailureClass.TIMEOUT,
        )
