"""Thin worker boundary for claiming a supported LangGraph AgentRun."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import session_scope
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim
from aidison.runtime.graphs import GraphRegistry


@dataclass(frozen=True, slots=True)
class ClaimedGraphRun:
    """A current lease plus its exact compiled graph; execution comes in R0-05."""

    run: AgentRun
    claim: AgentRunClaim
    compiled_graph: object


class LangGraphOrchestrationWorker:
    """Claims only Runs registered by this deployment at startup."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: GraphRegistry,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    async def claim_once(self, *, run_id: UUID | None = None) -> ClaimedGraphRun | None:
        """Return one executable Run, leaving incompatible Runs untouched."""

        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            claim = await control.claim_next(
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                support=self._registry.worker_support,
                run_id=run_id,
            )
            if claim is None:
                return None
            run = await control.get(claim.run_id)
            if run is None:  # pragma: no cover - same transaction makes this impossible
                raise RuntimeError("claimed AgentRun disappeared before execution")
            return ClaimedGraphRun(
                run=run,
                claim=claim,
                compiled_graph=self._registry.resolve(run.runtime_binding),
            )

    @asynccontextmanager
    async def lease_heartbeat(self, claim: AgentRunClaim) -> AsyncIterator[None]:
        """Keep a long-running graph claim alive until execution returns."""

        stop = asyncio.Event()
        interval = max(1.0, self._lease_seconds / 3)

        async def renew() -> None:
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    pass
                async with session_scope(self._session_factory) as session:
                    await AgentRunControl(session).renew_claim(
                        claim=claim,
                        lease_seconds=self._lease_seconds,
                    )

        heartbeat = asyncio.create_task(renew())
        try:
            yield
        finally:
            stop.set()
            await heartbeat
