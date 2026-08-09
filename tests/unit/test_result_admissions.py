from __future__ import annotations

import re
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest

from aidison.infrastructure.orm import ResultAdmissionRow
from aidison.infrastructure.result_admissions import (
    ResultAdmissionConflictError,
    ResultAdmissionRepository,
    admission_from_row,
    result_admission_key,
    result_admission_key_hash,
    row_from_admission,
)
from aidison.runtime.contracts import (
    FailureClass,
    HandoffResultStatus,
    ResultAdmission,
    ResultAdmissionStatus,
    ResultEnvelope,
    VerificationReceipt,
    VerificationStatus,
)


def basis() -> str:
    return sha256(b"basis").hexdigest()


def admitted(handoff_id: UUID, result_ref: str, confidence: float = 0.9) -> ResultAdmission:
    result = ResultEnvelope(
        handoff_id=handoff_id,
        basis_hash=basis(),
        status=HandoffResultStatus.SUCCEEDED,
        result_ref=result_ref,
        confidence=confidence,
    )
    receipt = VerificationReceipt(
        handoff_id=handoff_id,
        result_ref=result_ref,
        basis_hash=basis(),
        status=VerificationStatus.ADMITTED,
    )
    return ResultAdmission(result=result, receipt=receipt, status=ResultAdmissionStatus.ADMITTED)


def quarantined(handoff_id: UUID, result_ref: str) -> ResultAdmission:
    result = ResultEnvelope(
        handoff_id=handoff_id,
        basis_hash=basis(),
        status=HandoffResultStatus.SUCCEEDED,
        result_ref=result_ref,
        confidence=0.9,
    )
    receipt = VerificationReceipt(
        handoff_id=handoff_id,
        result_ref=result_ref,
        basis_hash=basis(),
        status=VerificationStatus.REJECTED,
        failure_class=FailureClass.EVIDENCE_CONFLICT,
        reasons=("证据与预算约束冲突",),
    )
    return ResultAdmission(result=result, receipt=receipt, status=ResultAdmissionStatus.QUARANTINED)


class FakeSession:
    """In-memory stand-in for the repository's exact query shape.

    ``scalar`` understands only ``select(ResultAdmissionRow).where(
    ResultAdmissionRow.key_hash == value)``; ``add`` queues an insert and ``commit``
    makes it visible, mirroring transaction semantics closely enough to exercise
    idempotency and conflict detection without a database.
    """

    def __init__(self) -> None:
        self._committed: dict[str, ResultAdmissionRow] = {}
        self._visible: dict[str, ResultAdmissionRow] = {}
        self._pending: list[ResultAdmissionRow] = []

    async def scalar(self, statement: Any) -> ResultAdmissionRow | None:
        value = statement.whereclause.right.value
        return self._visible.get(value)

    def add(self, row: ResultAdmissionRow) -> None:
        self._pending.append(row)

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def flush(self, rows: list[ResultAdmissionRow]) -> None:
        for row in rows:
            self._visible[row.key_hash] = row
            self._pending.remove(row)

    async def commit(self) -> None:
        for row in self._pending:
            self._committed[row.key_hash] = row
            self._visible[row.key_hash] = row
        self._committed.update(self._visible)
        self._pending.clear()

    @property
    def stored(self) -> dict[str, ResultAdmissionRow]:
        return dict(self._committed)


def test_result_admission_key_is_deterministic() -> None:
    handoff_id = uuid4()
    key = result_admission_key(handoff_id, "artifact+sha256://result/a", basis())
    assert key == result_admission_key(handoff_id, "artifact+sha256://result/a", basis())


def test_result_admission_key_binds_all_three_parts() -> None:
    handoff_id = uuid4()
    result_ref = "artifact+sha256://result/a"
    base = result_admission_key(handoff_id, result_ref, basis())
    assert base != result_admission_key(uuid4(), result_ref, basis())
    assert base != result_admission_key(handoff_id, result_ref + "/x", basis())
    assert base != result_admission_key(handoff_id, result_ref, "a" * 64)


def test_result_admission_key_hash_is_stable_sha256_hex() -> None:
    handoff_id = uuid4()
    result_ref = "artifact+sha256://result/a"
    first = result_admission_key_hash(handoff_id, result_ref, basis())
    replay = result_admission_key_hash(handoff_id, result_ref, basis())
    assert first == replay
    assert re.fullmatch(r"[a-f0-9]{64}", first) is not None
    assert first != result_admission_key_hash(handoff_id, result_ref, "b" * 64)


def test_row_round_trips_full_admission_payload() -> None:
    admission = admitted(uuid4(), "artifact+sha256://result/roundtrip")
    row = row_from_admission(admission)
    restored = admission_from_row(row)
    assert restored == admission
    assert restored.status is ResultAdmissionStatus.ADMITTED
    assert restored.result.handoff_id == admission.result.handoff_id
    assert restored.receipt.result_ref == admission.receipt.result_ref


@pytest.mark.asyncio
async def test_admit_persists_and_replays_idempotently() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    admission = admitted(uuid4(), "artifact+sha256://result/1")

    first = await repository.admit(admission)
    replay = await repository.admit(admission)

    assert replay == first == admission
    assert len(session.stored) == 1


@pytest.mark.asyncio
async def test_admit_returns_stored_record_for_equal_replay() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    admission = admitted(uuid4(), "artifact+sha256://result/2")

    await repository.admit(admission)
    equivalent = admitted(admission.result.handoff_id, admission.result.result_ref, confidence=0.9)
    returned = await repository.admit(equivalent)

    assert returned == admission
    assert returned is not admission
    assert len(session.stored) == 1


@pytest.mark.asyncio
async def test_admit_conflicts_on_different_status() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    handoff_id = uuid4()
    result_ref = "artifact+sha256://result/conflict-status"

    await repository.admit(admitted(handoff_id, result_ref))
    with pytest.raises(ResultAdmissionConflictError, match="conflicts"):
        await repository.admit(quarantined(handoff_id, result_ref))


@pytest.mark.asyncio
async def test_admit_conflicts_on_different_payload() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    handoff_id = uuid4()
    result_ref = "artifact+sha256://result/conflict-payload"

    await repository.admit(admitted(handoff_id, result_ref, confidence=0.9))
    with pytest.raises(ResultAdmissionConflictError, match="conflicts"):
        await repository.admit(admitted(handoff_id, result_ref, confidence=0.5))


@pytest.mark.asyncio
async def test_conflict_never_overwrites_stored_row() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    handoff_id = uuid4()
    result_ref = "artifact+sha256://result/never-overwrite"

    original = await repository.admit(admitted(handoff_id, result_ref))
    with pytest.raises(ResultAdmissionConflictError):
        await repository.admit(quarantined(handoff_id, result_ref))

    stored = await repository.get(handoff_id, result_ref, basis())
    assert stored == original
    assert stored.status is ResultAdmissionStatus.ADMITTED


@pytest.mark.asyncio
async def test_get_returns_stored_admission_and_none_for_missing() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    handoff_id = uuid4()
    result_ref = "artifact+sha256://result/get"

    missing = await repository.get(handoff_id, result_ref, basis())
    assert missing is None

    await repository.admit(admitted(handoff_id, result_ref))
    stored = await repository.get(handoff_id, result_ref, basis())
    assert stored == admitted(handoff_id, result_ref)
    assert await repository.get(handoff_id, result_ref + "/other", basis()) is None


@pytest.mark.asyncio
async def test_admit_without_commit_is_not_persisted() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    admission = admitted(uuid4(), "artifact+sha256://result/uncommitted")

    returned = await repository.admit(admission, commit=False)
    assert returned == admission
    assert session.stored == {}


@pytest.mark.asyncio
async def test_admit_without_commit_replays_idempotently_in_same_session() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    admission = admitted(uuid4(), "artifact+sha256://result/uncommitted-replay")

    first = await repository.admit(admission, commit=False)
    replay = await repository.admit(admission, commit=False)

    assert first == replay == admission
    assert len(session._pending) == 0
    assert session.stored == {}


@pytest.mark.asyncio
async def test_same_session_uncommitted_conflict_never_overwrites_visible_row() -> None:
    session = FakeSession()
    repository = ResultAdmissionRepository(session)  # type: ignore[arg-type]
    handoff_id = uuid4()
    result_ref = "artifact+sha256://result/uncommitted-conflict"

    original = admitted(handoff_id, result_ref)
    await repository.admit(original, commit=False)
    with pytest.raises(ResultAdmissionConflictError, match="conflicts"):
        await repository.admit(admitted(handoff_id, result_ref, confidence=0.5), commit=False)

    assert await repository.get(handoff_id, result_ref, basis()) == original
