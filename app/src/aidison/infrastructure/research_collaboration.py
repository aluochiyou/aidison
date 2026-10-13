"""Durable append-only storage for in-run research collaboration leads."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import ResearchCollaborationObservationRow

if TYPE_CHECKING:
    from aidison.research.collaboration import ResearchCollaborationObservation


class ResearchCollaborationConflictError(RuntimeError):
    """A producer retried an idempotency key with different immutable content."""


class ResearchCollaborationStore:
    """Store non-authoritative peer observations independently of Result admission."""

    def __init__(
        self,
        session: AsyncSession,
    ) -> None:
        self._session = session

    async def append(
        self, observation: ResearchCollaborationObservation
    ) -> ResearchCollaborationObservation:
        """Append once, returning the durable database sequence.

        The observation's payload is an immutable Artifact.  The row only
        stores enough indexed metadata for bounded catch-up and never becomes
        a parallel project-facts store.
        """

        session = self._session
        existing = await session.scalar(
            select(ResearchCollaborationObservationRow)
            .where(
                ResearchCollaborationObservationRow.idempotency_key
                == observation.idempotency_key
            )
            .with_for_update()
        )
        if existing is not None:
            restored = _from_row(existing)
            _require_same_observation(restored, observation)
            return restored
        row = ResearchCollaborationObservationRow(
            id=observation.id,
            project_id=observation.project_id,
            agent_run_id=observation.agent_run_id,
            basis_hash=observation.basis_hash,
            producer_task_id=observation.producer_task_id,
            kind=observation.kind.value,
            artifact_ref=observation.artifact_ref,
            content_hash=observation.content_hash,
            idempotency_key=observation.idempotency_key,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush([row])
        except IntegrityError:
            existing = await session.scalar(
                select(ResearchCollaborationObservationRow)
                .where(
                    ResearchCollaborationObservationRow.idempotency_key
                    == observation.idempotency_key
                )
                .with_for_update()
            )
            if existing is not None:
                restored = _from_row(existing)
                _require_same_observation(restored, observation)
                return restored
            raise
        return _from_row(row)

    async def list_after(
        self,
        *,
        agent_run_id: UUID,
        basis_hash: str,
        consumer_task_id: UUID,
        after_sequence: int,
        limit: int,
    ) -> tuple[ResearchCollaborationObservation, ...]:
        """Read committed peer leads after a durable cursor.

        ``consumer_task_id`` is deliberately excluded: a task never feeds its
        own discovery message back into its prompt.  Result admission and
        project facts remain entirely separate from this stream.
        """

        if after_sequence < 0:
            raise ValueError("collaboration cursor must be non-negative")
        if not 1 <= limit <= 64:
            raise ValueError("collaboration read limit must be between 1 and 64")
        session = self._session
        rows = (
            await session.scalars(
                select(ResearchCollaborationObservationRow)
                .where(
                    ResearchCollaborationObservationRow.agent_run_id == agent_run_id,
                    ResearchCollaborationObservationRow.basis_hash == basis_hash,
                    ResearchCollaborationObservationRow.producer_task_id != consumer_task_id,
                    ResearchCollaborationObservationRow.sequence > after_sequence,
                )
                .order_by(ResearchCollaborationObservationRow.sequence.asc())
                .limit(limit)
            )
        ).all()
        return tuple(_from_row(item) for item in rows)

def _from_row(row: ResearchCollaborationObservationRow) -> ResearchCollaborationObservation:
    from aidison.research.collaboration import (
        ResearchCollaborationKind,
        ResearchCollaborationObservation,
    )

    return ResearchCollaborationObservation(
        id=row.id,
        project_id=row.project_id,
        agent_run_id=row.agent_run_id,
        basis_hash=row.basis_hash,
        producer_task_id=row.producer_task_id,
        kind=ResearchCollaborationKind(row.kind),
        artifact_ref=row.artifact_ref,
        content_hash=row.content_hash,
        idempotency_key=row.idempotency_key,
        sequence=row.sequence,
    )


def _require_same_observation(
    stored: ResearchCollaborationObservation,
    requested: ResearchCollaborationObservation,
) -> None:
    left = stored.model_dump(mode="json", exclude={"id", "sequence"})
    right = requested.model_dump(mode="json", exclude={"id", "sequence"})
    if left != right:
        raise ResearchCollaborationConflictError(
            "research collaboration idempotency key has another payload"
        )


__all__ = ["ResearchCollaborationConflictError", "ResearchCollaborationStore"]
