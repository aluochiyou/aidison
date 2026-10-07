"""PostgreSQL repository for secret-free invocation recordings."""

from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import InvocationRecordingRow
from aidison.runtime.contracts import (
    BudgetOperationKind,
    FailureClass,
    InvocationRecording,
    InvocationRecordingStatus,
)
from aidison.runtime.replay import ReplayConflictError


def recording_from_row(row: InvocationRecordingRow) -> InvocationRecording:
    return InvocationRecording(
        project_id=row.project_id,
        agent_run_id=row.agent_run_id,
        producer_attempt_id=row.producer_attempt_id,
        basis_hash=row.basis_hash,
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        kind=BudgetOperationKind(row.kind),
        provider=row.provider,
        operation_name=row.operation_name,
        status=InvocationRecordingStatus(row.status),
        response_artifact_ref=row.response_artifact_ref,
        failure_class=None if row.failure_class is None else FailureClass(row.failure_class),
    )


class InvocationRecordingRepository:
    """Prepared/terminal store used by :class:`ReplayController`.

    Terminal recordings are immutable. A pending reservation may transition once
    to a terminal state, or be removed only when no provider dispatch occurred.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, idempotency_key: str) -> InvocationRecording | None:
        row = cast(
            InvocationRecordingRow | None,
            await self._session.scalar(
                select(InvocationRecordingRow).where(
                    InvocationRecordingRow.idempotency_key == idempotency_key
                )
            ),
        )
        return None if row is None else recording_from_row(row)

    async def list_for_run(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
    ) -> tuple[InvocationRecording, ...]:
        """Return the secret-free invocation ledger in deterministic order."""
        rows = (
            await self._session.scalars(
                select(InvocationRecordingRow)
                .where(
                    InvocationRecordingRow.project_id == project_id,
                    InvocationRecordingRow.agent_run_id == agent_run_id,
                )
                .order_by(InvocationRecordingRow.created_at, InvocationRecordingRow.id)
            )
        ).all()
        return tuple(recording_from_row(row) for row in rows)

    async def record(self, recording: InvocationRecording) -> InvocationRecording:
        if recording.status is InvocationRecordingStatus.PENDING:
            raise ReplayConflictError("use prepare to persist a pending invocation")
        row = cast(
            InvocationRecordingRow | None,
            await self._session.scalar(
                select(InvocationRecordingRow)
                .where(InvocationRecordingRow.idempotency_key == recording.idempotency_key)
                .with_for_update()
            ),
        )
        if row is not None:
            existing = recording_from_row(row)
            if existing == recording:
                return existing
            if existing.status is InvocationRecordingStatus.PENDING and _same_request(
                existing, recording
            ):
                row.status = recording.status.value
                row.response_artifact_ref = recording.response_artifact_ref
                row.failure_class = (
                    None if recording.failure_class is None else recording.failure_class.value
                )
                await self._session.commit()
                return recording
            raise ReplayConflictError("invocation recording replay conflicts with stored content")
        raise ReplayConflictError("invocation recording was not prepared")

    async def prepare(self, recording: InvocationRecording) -> bool:
        if recording.status is not InvocationRecordingStatus.PENDING:
            raise ReplayConflictError("prepared invocation must have pending status")
        existing = await self.get(recording.idempotency_key)
        if existing is not None:
            if _same_request(existing, recording):
                return False
            raise ReplayConflictError("invocation preparation conflicts with stored content")
        row = InvocationRecordingRow(
            id=uuid4(),
            project_id=recording.project_id,
            agent_run_id=recording.agent_run_id,
            producer_attempt_id=recording.producer_attempt_id,
            basis_hash=recording.basis_hash,
            idempotency_key=recording.idempotency_key,
            request_hash=recording.request_hash,
            kind=recording.kind.value,
            provider=recording.provider,
            operation_name=recording.operation_name,
            status=recording.status.value,
            response_artifact_ref=recording.response_artifact_ref,
            failure_class=(
                None if recording.failure_class is None else recording.failure_class.value
            ),
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush([row])
        except IntegrityError:
            existing = await self.get(recording.idempotency_key)
            if existing is not None:
                if _same_request(existing, recording):
                    return False
                raise ReplayConflictError(
                    "invocation preparation conflicts with stored content"
                ) from None
            raise
        await self._session.commit()
        return True

    async def discard_prepared(self, recording: InvocationRecording) -> None:
        if recording.status is not InvocationRecordingStatus.PENDING:
            raise ReplayConflictError("only pending invocation can be discarded")
        row = cast(
            InvocationRecordingRow | None,
            await self._session.scalar(
                select(InvocationRecordingRow)
                .where(InvocationRecordingRow.idempotency_key == recording.idempotency_key)
                .with_for_update()
            ),
        )
        if row is None:
            return
        existing = recording_from_row(row)
        if not _same_request(existing, recording):
            raise ReplayConflictError("invocation discard conflicts with stored content")
        if existing.status is not InvocationRecordingStatus.PENDING:
            raise ReplayConflictError("only pending invocation can be discarded")
        await self._session.delete(row)
        await self._session.commit()


def _same_request(left: InvocationRecording, right: InvocationRecording) -> bool:
    return (
        left.project_id == right.project_id
        and left.agent_run_id == right.agent_run_id
        and left.basis_hash == right.basis_hash
        and left.idempotency_key == right.idempotency_key
        and left.request_hash == right.request_hash
        and left.kind is right.kind
        and left.provider == right.provider
        and left.operation_name == right.operation_name
    )
