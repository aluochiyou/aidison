"""Bridge a claimed AgentRun to a LangGraph interrupt checkpoint.

The bridge deliberately has a small crash window: LangGraph can durably write a
checkpoint before Control admits its reference.  Recovery trusts only the
admitted reference, so that window fails closed instead of resuming a physical
latest checkpoint that was never accepted by Aidison control policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.application.langgraph_worker import ClaimedGraphRun
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import session_scope
from aidison.runtime.agent_runs import AdmittedCheckpointRef, AgentRun
from aidison.runtime.minimal_graph import (
    admitted_checkpoint_from_snapshot,
    execution_thread_config,
)


@dataclass(frozen=True, slots=True)
class InterruptedGraphRun:
    run: AgentRun
    checkpoint: AdmittedCheckpointRef
    output: dict[str, Any]


class LangGraphCheckpointBridge:
    """Execute up to an interrupt, then atomically admit its recovery anchor."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def run_to_interrupt(
        self,
        *,
        claimed: ClaimedGraphRun,
        input_state: dict[str, Any],
    ) -> InterruptedGraphRun:
        expected_run_id = str(claimed.run.id)
        if input_state.get("run_id") != expected_run_id:
            raise ValueError("LangGraph input must carry the claimed AgentRun id")

        graph = cast(Any, claimed.compiled_graph)
        config = execution_thread_config(
            thread_id=claimed.run.thread_id,
            generation=claimed.claim.generation,
        )
        output = cast(dict[str, Any], await graph.ainvoke(input_state, config))
        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts:
            raise RuntimeError("checkpoint bridge expected a LangGraph interrupt")
        checkpoint = admitted_checkpoint_from_snapshot(
            snapshot=snapshot,
            binding=claimed.run.runtime_binding,
            generation=claimed.claim.generation,
        )

        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            await control.admit_checkpoint(claim=claimed.claim, checkpoint=checkpoint)
            waiting_run = await control.wait_for_decision(claim=claimed.claim)
        return InterruptedGraphRun(
            run=waiting_run,
            checkpoint=checkpoint,
            output=output,
        )
