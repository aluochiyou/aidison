from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.signals import PostgresSignalBus, SignalUnavailableError
from aidison.runtime.contracts import JobClaim, JoinSnapshot


class DurableJoinWaiter:
    """Wait for a durable Join, using notifications only as a latency optimization."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        signal_bus: PostgresSignalBus,
        durable_recheck_seconds: float = 30,
        signal_failure_recheck_seconds: float = 0.5,
    ) -> None:
        if durable_recheck_seconds <= 0:
            raise ValueError("durable_recheck_seconds must be positive")
        if signal_failure_recheck_seconds <= 0:
            raise ValueError("signal_failure_recheck_seconds must be positive")
        self._factory = session_factory
        self._signal_bus = signal_bus
        self._durable_recheck_seconds = durable_recheck_seconds
        self._signal_failure_recheck_seconds = signal_failure_recheck_seconds

    async def wait(
        self,
        *,
        project_id: UUID,
        join_group_id: UUID,
        parent_claim: JobClaim,
    ) -> JoinSnapshot:
        while True:
            try:
                async with self._signal_bus.subscribe(project_id=project_id) as subscription:
                    while True:
                        snapshot = await self._inspect(
                            join_group_id=join_group_id,
                            parent_claim=parent_claim,
                        )
                        if snapshot.ready or snapshot.impossible:
                            return snapshot
                        await subscription.wait(
                            timeout_seconds=self._durable_recheck_seconds,
                        )
            except SignalUnavailableError:
                snapshot = await self._inspect(
                    join_group_id=join_group_id,
                    parent_claim=parent_claim,
                )
                if snapshot.ready or snapshot.impossible:
                    return snapshot
                await asyncio.sleep(self._signal_failure_recheck_seconds)

    async def _inspect(
        self,
        *,
        join_group_id: UUID,
        parent_claim: JobClaim,
    ) -> JoinSnapshot:
        async with self._factory() as session:
            return await PostgresRuntime(session).inspect_join(
                join_group_id=join_group_id,
                parent_claim=parent_claim,
            )
