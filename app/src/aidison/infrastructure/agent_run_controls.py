"""Persistence for typed AgentRun pause and steering requests."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.agent_runs import AgentRunConflictError
from aidison.infrastructure.orm import AgentRunControlRequestRow
from aidison.runtime.control_requests import (
    AgentRunControlRequest,
    ControlRequestStatus,
    utc_now,
)


class AgentRunControlRequestStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def request(self, value: AgentRunControlRequest) -> AgentRunControlRequest:
        existing = await self._session.scalar(
            select(AgentRunControlRequestRow).where(
                AgentRunControlRequestRow.idempotency_key == value.idempotency_key
            )
        )
        if existing is not None:
            restored = self._from_row(existing)
            if not self._same_request(restored, value):
                raise AgentRunConflictError("control request idempotency key has another payload")
            return restored
        row = AgentRunControlRequestRow(
            id=value.id,
            agent_run_id=value.agent_run_id,
            kind=value.kind.value,
            basis_hash=value.basis_hash,
            payload=value.payload,
            idempotency_key=value.idempotency_key,
            status=value.status.value,
            created_at=value.created_at,
            acknowledged_at=value.acknowledged_at,
        )
        try:
            # An API client can retry the same command while its first request
            # is still committing.  The lookup above is intentionally cheap,
            # but cannot close that race; confine the unique-key conflict to a
            # savepoint and re-read the immutable command receipt afterwards.
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush([row])
        except IntegrityError:
            existing = await self._session.scalar(
                select(AgentRunControlRequestRow)
                .where(
                    AgentRunControlRequestRow.idempotency_key == value.idempotency_key
                )
                .with_for_update()
            )
            if existing is None:
                raise
            restored = self._from_row(existing)
            if not self._same_request(restored, value):
                raise AgentRunConflictError(
                    "control request idempotency key has another payload"
                ) from None
            return restored
        return value

    async def list_for_run(self, *, agent_run_id: UUID) -> tuple[AgentRunControlRequest, ...]:
        rows = list(
            (
                await self._session.scalars(
                    select(AgentRunControlRequestRow)
                    .where(AgentRunControlRequestRow.agent_run_id == agent_run_id)
                    .order_by(AgentRunControlRequestRow.created_at, AgentRunControlRequestRow.id)
                )
            ).all()
        )
        return tuple(self._from_row(row) for row in rows)

    async def acknowledge(
        self, request_id: UUID, *, rejected: bool = False
    ) -> AgentRunControlRequest:
        row = await self._session.scalar(
            select(AgentRunControlRequestRow)
            .where(AgentRunControlRequestRow.id == request_id)
            .with_for_update()
        )
        if row is None:
            raise AgentRunConflictError("control request not found")
        if row.status == ControlRequestStatus.REQUESTED.value:
            row.status = (
                ControlRequestStatus.REJECTED.value
                if rejected
                else ControlRequestStatus.ACKNOWLEDGED.value
            )
            row.acknowledged_at = utc_now()
            await self._session.flush()
        return self._from_row(row)

    @staticmethod
    def _from_row(row: AgentRunControlRequestRow) -> AgentRunControlRequest:
        return AgentRunControlRequest.model_validate(
            {
                "id": row.id,
                "agent_run_id": row.agent_run_id,
                "kind": row.kind,
                "basis_hash": row.basis_hash,
                "payload": row.payload,
                "idempotency_key": row.idempotency_key,
                "status": row.status,
                "created_at": row.created_at,
                "acknowledged_at": row.acknowledged_at,
            }
        )

    @staticmethod
    def _same_request(
        left: AgentRunControlRequest,
        right: AgentRunControlRequest,
    ) -> bool:
        return (
            left.agent_run_id == right.agent_run_id
            and left.kind == right.kind
            and left.basis_hash == right.basis_hash
            and left.payload == right.payload
            and left.idempotency_key == right.idempotency_key
        )
