"""Claimed Impact AgentRun adapter over the shared LangGraph runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.application.langgraph_worker import LangGraphOrchestrationWorker
from aidison.application.single_task_research import AgentRunCancelled
from aidison.impact.execution import ImpactRunExecutor
from aidison.impact.graph import build_impact_graph
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import session_scope
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim, AgentRunKind, AgentRunStatus
from aidison.runtime.minimal_graph import thread_config


@dataclass(frozen=True, slots=True)
class ImpactRunExecution:
    """Public terminal outcome after one bounded deterministic ImpactGraph run."""

    status: str
    report_manifest_ref: str | None


class ImpactGraphRunExecutor:
    """Execute and terminally close one claimed Impact Run."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
        checkpointer: BaseCheckpointSaver[Any],
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._checkpointer = checkpointer

    async def execute_claim(self, *, run: AgentRun, claim: AgentRunClaim) -> ImpactRunExecution:
        if run.kind is not AgentRunKind.IMPACT:
            raise RuntimeError("claimed AgentRun is not an Impact run")
        graph = build_impact_graph(
            checkpointer=self._checkpointer,
            executor=ImpactRunExecutor(
                session_factory=self._session_factory,
                artifact_root=self._artifact_root,
            ),
            run=run,
            claim=claim,
        )
        try:
            state = await graph.ainvoke(
                {"run_id": str(run.id)},
                thread_config(thread_id=run.thread_id),
            )
        except AgentRunCancelled:
            return ImpactRunExecution(status="cancelled", report_manifest_ref=None)
        except Exception:
            await self._fail_claim_if_current(claim=claim)
            raise
        report_ref = state.get("report_manifest_ref")
        if not isinstance(report_ref, str) or not report_ref:
            await self._fail_claim_if_current(claim=claim)
            raise RuntimeError("ImpactGraph ended without a report manifest")
        async with session_scope(self._session_factory) as session:
            await AgentRunControl(session).complete(claim=claim, status=AgentRunStatus.SUCCEEDED)
        return ImpactRunExecution(status="succeeded", report_manifest_ref=report_ref)

    async def _fail_claim_if_current(self, *, claim: AgentRunClaim) -> None:
        try:
            async with session_scope(self._session_factory) as session:
                await AgentRunControl(session).complete(claim=claim, status=AgentRunStatus.FAILED)
        except Exception:
            return


class ImpactLangGraphWorker:
    """Consume only Impact Runs matching this deployment's frozen binding."""

    def __init__(self, *, orchestration_worker: LangGraphOrchestrationWorker) -> None:
        self._orchestration_worker = orchestration_worker

    async def run_once(self) -> ImpactRunExecution | None:
        claimed = await self._orchestration_worker.claim_once()
        if claimed is None:
            return None
        if claimed.run.kind is not AgentRunKind.IMPACT:
            raise RuntimeError("Impact worker claimed a non-Impact AgentRun")
        if not isinstance(claimed.compiled_graph, ImpactGraphRunExecutor):
            raise RuntimeError("Impact runtime binding has no ImpactGraphRunExecutor registration")
        async with self._orchestration_worker.lease_heartbeat(claimed.claim):
            return await claimed.compiled_graph.execute_claim(run=claimed.run, claim=claimed.claim)


__all__ = ["ImpactGraphRunExecutor", "ImpactLangGraphWorker", "ImpactRunExecution"]
