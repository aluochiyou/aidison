"""Persistence for verified event-replay snapshots.

Snapshots accelerate replay only.  They are immutable records that can be
discarded and rebuilt from ``domain_events``; the repository deliberately has
no update or delete operation.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.domain.events import ExecutionPlanReplaySnapshot
from aidison.infrastructure.orm import EventReplaySnapshotRow
from aidison.runtime.agent_result_events import AgentResultReplaySnapshot
from aidison.runtime.agent_run_decision_events import AgentRunDecisionReplaySnapshot
from aidison.runtime.agent_run_effect_events import AgentRunEffectReplaySnapshot
from aidison.runtime.agent_run_events import AgentRunReplaySnapshot


class ReplaySnapshotConflictError(RuntimeError):
    """An immutable snapshot identity already refers to different state."""


def execution_plan_snapshot_from_row(
    row: EventReplaySnapshotRow,
) -> ExecutionPlanReplaySnapshot:
    if row.aggregate_type != "execution_plan":
        raise ReplaySnapshotConflictError("snapshot row is not an execution-plan snapshot")
    return ExecutionPlanReplaySnapshot(
        project_id=row.project_id,
        aggregate_id=row.aggregate_id,
        aggregate_version=row.aggregate_version,
        project_seq=row.project_seq,
        reducer_version=row.reducer_version,
        state=row.state,
        state_hash=row.state_hash,
        created_at=row.created_at,
    )


def agent_run_snapshot_from_row(row: EventReplaySnapshotRow) -> AgentRunReplaySnapshot:
    if row.aggregate_type != "agent_run":
        raise ReplaySnapshotConflictError("snapshot row is not an AgentRun replay snapshot")
    return AgentRunReplaySnapshot(
        project_id=row.project_id,
        aggregate_id=row.aggregate_id,
        aggregate_version=row.aggregate_version,
        project_seq=row.project_seq,
        reducer_version=row.reducer_version,
        state=row.state,
        state_hash=row.state_hash,
        created_at=row.created_at,
    )


def agent_result_snapshot_from_row(row: EventReplaySnapshotRow) -> AgentResultReplaySnapshot:
    if row.aggregate_type != "agent_run_result":
        raise ReplaySnapshotConflictError("snapshot row is not an AgentRun result replay snapshot")
    return AgentResultReplaySnapshot(
        project_id=row.project_id,
        aggregate_id=row.aggregate_id,
        aggregate_version=row.aggregate_version,
        project_seq=row.project_seq,
        reducer_version=row.reducer_version,
        state=row.state,
        state_hash=row.state_hash,
        created_at=row.created_at,
    )


def agent_run_decision_snapshot_from_row(
    row: EventReplaySnapshotRow,
) -> AgentRunDecisionReplaySnapshot:
    if row.aggregate_type != "agent_run_decision":
        raise ReplaySnapshotConflictError("snapshot row is not an AgentRun decision snapshot")
    return AgentRunDecisionReplaySnapshot(
        project_id=row.project_id,
        aggregate_id=row.aggregate_id,
        aggregate_version=row.aggregate_version,
        project_seq=row.project_seq,
        reducer_version=row.reducer_version,
        state=row.state,
        state_hash=row.state_hash,
        created_at=row.created_at,
    )


def agent_run_effect_snapshot_from_row(
    row: EventReplaySnapshotRow,
) -> AgentRunEffectReplaySnapshot:
    if row.aggregate_type != "agent_run_effect":
        raise ReplaySnapshotConflictError("snapshot row is not an AgentRun effect snapshot")
    return AgentRunEffectReplaySnapshot(
        project_id=row.project_id,
        aggregate_id=row.aggregate_id,
        aggregate_version=row.aggregate_version,
        project_seq=row.project_seq,
        reducer_version=row.reducer_version,
        state=row.state,
        state_hash=row.state_hash,
        created_at=row.created_at,
    )


class EventReplaySnapshotRepository:
    """Append-only repository for verified aggregate replay snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest_execution_plan(
        self,
        *,
        project_id: UUID,
        aggregate_id: UUID,
    ) -> ExecutionPlanReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow)
                .where(
                    EventReplaySnapshotRow.project_id == project_id,
                    EventReplaySnapshotRow.aggregate_type == "execution_plan",
                    EventReplaySnapshotRow.aggregate_id == aggregate_id,
                )
                .order_by(
                    EventReplaySnapshotRow.aggregate_version.desc(),
                    EventReplaySnapshotRow.created_at.desc(),
                )
                .limit(1)
            ),
        )
        return None if row is None else execution_plan_snapshot_from_row(row)

    async def persist_execution_plan(
        self,
        snapshot: ExecutionPlanReplaySnapshot,
    ) -> ExecutionPlanReplaySnapshot:
        """Insert an immutable snapshot or return an identical retry result."""
        row = EventReplaySnapshotRow(
            project_id=snapshot.project_id,
            aggregate_type="execution_plan",
            aggregate_id=snapshot.aggregate_id,
            aggregate_version=snapshot.aggregate_version,
            project_seq=snapshot.project_seq,
            reducer_version=snapshot.reducer_version,
            state=snapshot.state,
            state_hash=snapshot.state_hash,
            created_at=snapshot.created_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush([row])
        except IntegrityError:
            existing = await self._execution_plan_at_version(snapshot)
            if existing is not None and _same_snapshot_content(existing, snapshot):
                return existing
            raise ReplaySnapshotConflictError(
                "replay snapshot identity already has different immutable state"
            ) from None
        return snapshot

    async def get_latest_agent_run(
        self,
        *,
        project_id: UUID,
        aggregate_id: UUID,
    ) -> AgentRunReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow)
                .where(
                    EventReplaySnapshotRow.project_id == project_id,
                    EventReplaySnapshotRow.aggregate_type == "agent_run",
                    EventReplaySnapshotRow.aggregate_id == aggregate_id,
                )
                .order_by(
                    EventReplaySnapshotRow.aggregate_version.desc(),
                    EventReplaySnapshotRow.created_at.desc(),
                )
                .limit(1)
            ),
        )
        return None if row is None else agent_run_snapshot_from_row(row)

    async def persist_agent_run(
        self,
        snapshot: AgentRunReplaySnapshot,
    ) -> AgentRunReplaySnapshot:
        """Insert an immutable AgentRun snapshot or return an identical retry."""
        row = EventReplaySnapshotRow(
            project_id=snapshot.project_id,
            aggregate_type="agent_run",
            aggregate_id=snapshot.aggregate_id,
            aggregate_version=snapshot.aggregate_version,
            project_seq=snapshot.project_seq,
            reducer_version=snapshot.reducer_version,
            state=snapshot.state,
            state_hash=snapshot.state_hash,
            created_at=snapshot.created_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush([row])
        except IntegrityError:
            existing = await self._agent_run_at_version(snapshot)
            if existing is not None and _same_snapshot_content(existing, snapshot):
                return existing
            raise ReplaySnapshotConflictError(
                "replay snapshot identity already has different immutable state"
            ) from None
        return snapshot

    async def get_latest_agent_result(
        self,
        *,
        project_id: UUID,
        aggregate_id: UUID,
    ) -> AgentResultReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow)
                .where(
                    EventReplaySnapshotRow.project_id == project_id,
                    EventReplaySnapshotRow.aggregate_type == "agent_run_result",
                    EventReplaySnapshotRow.aggregate_id == aggregate_id,
                )
                .order_by(
                    EventReplaySnapshotRow.aggregate_version.desc(),
                    EventReplaySnapshotRow.created_at.desc(),
                )
                .limit(1)
            ),
        )
        return None if row is None else agent_result_snapshot_from_row(row)

    async def persist_agent_result(
        self,
        snapshot: AgentResultReplaySnapshot,
    ) -> AgentResultReplaySnapshot:
        """Insert an immutable AgentRun result snapshot or return an identical retry."""
        row = EventReplaySnapshotRow(
            project_id=snapshot.project_id,
            aggregate_type="agent_run_result",
            aggregate_id=snapshot.aggregate_id,
            aggregate_version=snapshot.aggregate_version,
            project_seq=snapshot.project_seq,
            reducer_version=snapshot.reducer_version,
            state=snapshot.state,
            state_hash=snapshot.state_hash,
            created_at=snapshot.created_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush([row])
        except IntegrityError:
            existing = await self._agent_result_at_version(snapshot)
            if existing is not None and _same_snapshot_content(existing, snapshot):
                return existing
            raise ReplaySnapshotConflictError(
                "replay snapshot identity already has different immutable state"
            ) from None
        return snapshot

    async def get_latest_agent_run_decision(
        self,
        *,
        project_id: UUID,
        aggregate_id: UUID,
    ) -> AgentRunDecisionReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow)
                .where(
                    EventReplaySnapshotRow.project_id == project_id,
                    EventReplaySnapshotRow.aggregate_type == "agent_run_decision",
                    EventReplaySnapshotRow.aggregate_id == aggregate_id,
                )
                .order_by(
                    EventReplaySnapshotRow.aggregate_version.desc(),
                    EventReplaySnapshotRow.created_at.desc(),
                )
                .limit(1)
            ),
        )
        return None if row is None else agent_run_decision_snapshot_from_row(row)

    async def persist_agent_run_decision(
        self,
        snapshot: AgentRunDecisionReplaySnapshot,
    ) -> AgentRunDecisionReplaySnapshot:
        """Insert an immutable proposal-decision snapshot or return an identical retry."""
        row = EventReplaySnapshotRow(
            project_id=snapshot.project_id,
            aggregate_type="agent_run_decision",
            aggregate_id=snapshot.aggregate_id,
            aggregate_version=snapshot.aggregate_version,
            project_seq=snapshot.project_seq,
            reducer_version=snapshot.reducer_version,
            state=snapshot.state,
            state_hash=snapshot.state_hash,
            created_at=snapshot.created_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush([row])
        except IntegrityError:
            existing = await self._agent_run_decision_at_version(snapshot)
            if existing is not None and _same_snapshot_content(existing, snapshot):
                return existing
            raise ReplaySnapshotConflictError(
                "replay snapshot identity already has different immutable state"
            ) from None
        return snapshot

    async def get_latest_agent_run_effect(
        self,
        *,
        project_id: UUID,
        aggregate_id: UUID,
    ) -> AgentRunEffectReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow)
                .where(
                    EventReplaySnapshotRow.project_id == project_id,
                    EventReplaySnapshotRow.aggregate_type == "agent_run_effect",
                    EventReplaySnapshotRow.aggregate_id == aggregate_id,
                )
                .order_by(
                    EventReplaySnapshotRow.aggregate_version.desc(),
                    EventReplaySnapshotRow.created_at.desc(),
                )
                .limit(1)
            ),
        )
        return None if row is None else agent_run_effect_snapshot_from_row(row)

    async def persist_agent_run_effect(
        self,
        snapshot: AgentRunEffectReplaySnapshot,
    ) -> AgentRunEffectReplaySnapshot:
        """Insert an immutable effect-ledger snapshot or return an identical retry."""
        row = EventReplaySnapshotRow(
            project_id=snapshot.project_id,
            aggregate_type="agent_run_effect",
            aggregate_id=snapshot.aggregate_id,
            aggregate_version=snapshot.aggregate_version,
            project_seq=snapshot.project_seq,
            reducer_version=snapshot.reducer_version,
            state=snapshot.state,
            state_hash=snapshot.state_hash,
            created_at=snapshot.created_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush([row])
        except IntegrityError:
            existing = await self._agent_run_effect_at_version(snapshot)
            if existing is not None and _same_snapshot_content(existing, snapshot):
                return existing
            raise ReplaySnapshotConflictError(
                "replay snapshot identity already has different immutable state"
            ) from None
        return snapshot

    async def _execution_plan_at_version(
        self,
        snapshot: ExecutionPlanReplaySnapshot,
    ) -> ExecutionPlanReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow).where(
                    EventReplaySnapshotRow.project_id == snapshot.project_id,
                    EventReplaySnapshotRow.aggregate_type == "execution_plan",
                    EventReplaySnapshotRow.aggregate_id == snapshot.aggregate_id,
                    EventReplaySnapshotRow.aggregate_version == snapshot.aggregate_version,
                    EventReplaySnapshotRow.reducer_version == snapshot.reducer_version,
                )
            ),
        )
        return None if row is None else execution_plan_snapshot_from_row(row)

    async def _agent_run_at_version(
        self,
        snapshot: AgentRunReplaySnapshot,
    ) -> AgentRunReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow).where(
                    EventReplaySnapshotRow.project_id == snapshot.project_id,
                    EventReplaySnapshotRow.aggregate_type == "agent_run",
                    EventReplaySnapshotRow.aggregate_id == snapshot.aggregate_id,
                    EventReplaySnapshotRow.aggregate_version == snapshot.aggregate_version,
                    EventReplaySnapshotRow.reducer_version == snapshot.reducer_version,
                )
            ),
        )
        return None if row is None else agent_run_snapshot_from_row(row)

    async def _agent_result_at_version(
        self,
        snapshot: AgentResultReplaySnapshot,
    ) -> AgentResultReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow).where(
                    EventReplaySnapshotRow.project_id == snapshot.project_id,
                    EventReplaySnapshotRow.aggregate_type == "agent_run_result",
                    EventReplaySnapshotRow.aggregate_id == snapshot.aggregate_id,
                    EventReplaySnapshotRow.aggregate_version == snapshot.aggregate_version,
                    EventReplaySnapshotRow.reducer_version == snapshot.reducer_version,
                )
            ),
        )
        return None if row is None else agent_result_snapshot_from_row(row)

    async def _agent_run_decision_at_version(
        self,
        snapshot: AgentRunDecisionReplaySnapshot,
    ) -> AgentRunDecisionReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow).where(
                    EventReplaySnapshotRow.project_id == snapshot.project_id,
                    EventReplaySnapshotRow.aggregate_type == "agent_run_decision",
                    EventReplaySnapshotRow.aggregate_id == snapshot.aggregate_id,
                    EventReplaySnapshotRow.aggregate_version == snapshot.aggregate_version,
                    EventReplaySnapshotRow.reducer_version == snapshot.reducer_version,
                )
            ),
        )
        return None if row is None else agent_run_decision_snapshot_from_row(row)

    async def _agent_run_effect_at_version(
        self,
        snapshot: AgentRunEffectReplaySnapshot,
    ) -> AgentRunEffectReplaySnapshot | None:
        row = cast(
            EventReplaySnapshotRow | None,
            await self._session.scalar(
                select(EventReplaySnapshotRow).where(
                    EventReplaySnapshotRow.project_id == snapshot.project_id,
                    EventReplaySnapshotRow.aggregate_type == "agent_run_effect",
                    EventReplaySnapshotRow.aggregate_id == snapshot.aggregate_id,
                    EventReplaySnapshotRow.aggregate_version == snapshot.aggregate_version,
                    EventReplaySnapshotRow.reducer_version == snapshot.reducer_version,
                )
            ),
        )
        return None if row is None else agent_run_effect_snapshot_from_row(row)


def _same_snapshot_content(
    left: (
        ExecutionPlanReplaySnapshot
        | AgentRunReplaySnapshot
        | AgentResultReplaySnapshot
        | AgentRunDecisionReplaySnapshot
        | AgentRunEffectReplaySnapshot
    ),
    right: (
        ExecutionPlanReplaySnapshot
        | AgentRunReplaySnapshot
        | AgentResultReplaySnapshot
        | AgentRunDecisionReplaySnapshot
        | AgentRunEffectReplaySnapshot
    ),
) -> bool:
    """Treat a retry as idempotent while preserving the first creation time."""
    return (
        left.project_id == right.project_id
        and left.aggregate_id == right.aggregate_id
        and left.aggregate_version == right.aggregate_version
        and left.project_seq == right.project_seq
        and left.reducer_version == right.reducer_version
        and left.state == right.state
        and left.state_hash == right.state_hash
    )
