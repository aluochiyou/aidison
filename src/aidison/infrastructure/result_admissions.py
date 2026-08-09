from __future__ import annotations

import hashlib
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import ResultAdmissionRow
from aidison.runtime.contracts import (
    ResultAdmission,
    ResultAdmissionStatus,
    ResultEnvelope,
    VerificationReceipt,
)


class ResultAdmissionConflictError(RuntimeError):
    """Raised when an admission replays an existing key with a different payload."""


def result_admission_key(handoff_id: UUID, result_ref: str, basis_hash: str) -> str:
    """Canonical, deterministic identity of one admission row.

    The key binds all three parts, so a replay that changes any of them resolves to
    a different row instead of silently overwriting the stored record.
    """

    return f"{handoff_id}:{result_ref}:{basis_hash}"


def result_admission_key_hash(handoff_id: UUID, result_ref: str, basis_hash: str) -> str:
    """Fixed-size content key derived from the admission's semantic identity."""

    return hashlib.sha256(
        result_admission_key(handoff_id, result_ref, basis_hash).encode()
    ).hexdigest()


def row_from_admission(admission: ResultAdmission) -> ResultAdmissionRow:
    """Project one ResultAdmission into its append-only row payload."""

    return ResultAdmissionRow(
        id=uuid4(),
        key_hash=result_admission_key_hash(
            admission.result.handoff_id,
            admission.result.result_ref,
            admission.result.basis_hash,
        ),
        handoff_id=admission.result.handoff_id,
        result_ref=admission.result.result_ref,
        basis_hash=admission.result.basis_hash,
        status=admission.status.value,
        result_payload=admission.result.model_dump(mode="json"),
        receipt_payload=admission.receipt.model_dump(mode="json"),
    )


def admission_from_row(row: ResultAdmissionRow) -> ResultAdmission:
    """Reconstruct the full admission from a stored row."""

    return ResultAdmission(
        result=ResultEnvelope.model_validate(row.result_payload),
        receipt=VerificationReceipt.model_validate(row.receipt_payload),
        status=ResultAdmissionStatus(row.status),
    )


class ResultAdmissionRepository:
    """Append-only PostgreSQL ledger for ResultAdmission + VerificationReceipt.

    The write key is derived deterministically from handoff_id, result_ref, and
    basis_hash. Replaying the same semantic object returns the stored record;
    replaying the same key with a different result/receipt payload or status raises
    ``ResultAdmissionConflictError`` and never overwrites the existing row.

    This adapter is deliberately not wired into any scheduler, retry loop, or the
    effect-approval boundary; the unique ``key_hash`` index is the final barrier
    against competing writers inside the same database.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lookup(self, key_hash: str) -> ResultAdmissionRow | None:
        return cast(
            ResultAdmissionRow | None,
            await self._session.scalar(
                select(ResultAdmissionRow).where(ResultAdmissionRow.key_hash == key_hash)
            ),
        )

    @staticmethod
    def _resolve_existing(
        row: ResultAdmissionRow, admission: ResultAdmission, key_hash: str
    ) -> ResultAdmission:
        stored = admission_from_row(row)
        if stored == admission:
            return stored
        raise ResultAdmissionConflictError(
            f"result admission replay conflicts for key {key_hash}: stored "
            f"{stored.status.value} does not match {admission.status.value}"
        )

    async def admit(self, admission: ResultAdmission, *, commit: bool = True) -> ResultAdmission:
        """Persist one admission exactly once, idempotently for equal replays."""
        key_hash = result_admission_key_hash(
            admission.result.handoff_id,
            admission.result.result_ref,
            admission.result.basis_hash,
        )
        row = await self._lookup(key_hash)
        if row is not None:
            return self._resolve_existing(row, admission, key_hash)

        candidate = row_from_admission(admission)
        try:
            # The savepoint rolls back only this insert when another transaction
            # wins the unique-key race, leaving the caller's outer transaction
            # usable. Flushing makes commit=False replays visible in this session.
            async with self._session.begin_nested():
                self._session.add(candidate)
                await self._session.flush([candidate])
        except IntegrityError:
            row = await self._lookup(key_hash)
            if row is None:
                raise ResultAdmissionConflictError(
                    f"result admission race for key {key_hash} did not yield a stored row"
                ) from None
            return self._resolve_existing(row, admission, key_hash)

        if commit:
            await self._session.commit()
        return admission

    async def get(
        self,
        handoff_id: UUID,
        result_ref: str,
        basis_hash: str,
    ) -> ResultAdmission | None:
        """Read one admission by its deterministic key, or None when absent."""
        row = await self._lookup(result_admission_key_hash(handoff_id, result_ref, basis_hash))
        return None if row is None else admission_from_row(row)
