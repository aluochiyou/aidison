"""Claimed ImpactGraph v2 execution: report, bounded analyst, then human review."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.application.impact_decision_bridge import ImpactDecisionBridge
from aidison.application.langgraph_worker import LangGraphOrchestrationWorker
from aidison.application.single_task_research import AgentRunCancelled
from aidison.impact.analyst import ImpactAnalyst
from aidison.impact.proposal_execution import ImpactProposalExecutor
from aidison.impact.proposal_graph import build_impact_proposal_graph
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import session_scope
from aidison.observability import (
    DisabledRuntimeTracer,
    LangGraphRuntimeCallback,
    RuntimeTracer,
    TelemetryCorrelation,
)
from aidison.research.decision_contracts import AgentRunDecision
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim, AgentRunKind, AgentRunStatus
from aidison.runtime.minimal_graph import thread_config


@dataclass(frozen=True, slots=True)
class ImpactProposalRunExecution:
    """The public outcome is a waiting decision, never a direct Domain mutation."""

    readiness: str
    agent_decision: AgentRunDecision | None


class ImpactProposalGraphRunExecutor:
    """Execute the v2 ImpactGraph for the exact registered runtime binding."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
        checkpointer: BaseCheckpointSaver[Any],
        analyst: ImpactAnalyst,
        runtime_tracer: RuntimeTracer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._checkpointer = checkpointer
        self._analyst = analyst
        self._runtime_tracer = runtime_tracer or DisabledRuntimeTracer()

    def _graph_callback(self, *, run: AgentRun) -> LangGraphRuntimeCallback:
        return LangGraphRuntimeCallback(
            tracer=self._runtime_tracer,
            correlation=TelemetryCorrelation(project_id=run.project_id, run_id=run.id),
            graph_name="impact_proposal",
            graph_revision=run.runtime_binding.graph_revision,
        )

    async def execute_claim(
        self, *, run: AgentRun, claim: AgentRunClaim
    ) -> ImpactProposalRunExecution:
        if run.kind is not AgentRunKind.IMPACT:
            raise RuntimeError("claimed AgentRun is not an Impact run")
        graph = build_impact_proposal_graph(
            checkpointer=self._checkpointer,
            executor=ImpactProposalExecutor(
                session_factory=self._session_factory,
                artifact_root=self._artifact_root,
                analyst=self._analyst,
            ),
            run=run,
            claim=claim,
        )
        try:
            callback = self._graph_callback(run=run)
            config: dict[str, Any] = thread_config(thread_id=run.thread_id)
            config["callbacks"] = [callback]
            try:
                state = await graph.ainvoke({"run_id": str(run.id)}, config)
            finally:
                callback.close()
        except AgentRunCancelled:
            return ImpactProposalRunExecution(readiness="cancelled", agent_decision=None)
        except Exception:
            await self._fail_claim_if_current(claim=claim)
            raise
        if state.get("proposal_readiness") != "ready":
            await self._fail_claim_if_current(claim=claim)
            raise RuntimeError("ImpactGraph ended without a READY proposal")
        decision, _ = await ImpactDecisionBridge(
            session_factory=self._session_factory,
            artifact_root=self._artifact_root,
        ).prepare_from_interrupt(run=run, claim=claim, graph=graph)
        return ImpactProposalRunExecution(readiness="ready", agent_decision=decision)

    async def _fail_claim_if_current(self, *, claim: AgentRunClaim) -> None:
        try:
            async with session_scope(self._session_factory) as session:
                await AgentRunControl(session).complete(claim=claim, status=AgentRunStatus.FAILED)
        except Exception:
            return


class ImpactProposalLangGraphWorker:
    """Consume only v2 ImpactGraph bindings; the legacy v1 executor stays isolated."""

    def __init__(self, *, orchestration_worker: LangGraphOrchestrationWorker) -> None:
        self._orchestration_worker = orchestration_worker

    async def run_once(self) -> ImpactProposalRunExecution | None:
        claimed = await self._orchestration_worker.claim_once()
        if claimed is None:
            return None
        if claimed.run.kind is not AgentRunKind.IMPACT:
            raise RuntimeError("Impact proposal worker claimed a non-Impact AgentRun")
        if not isinstance(claimed.compiled_graph, ImpactProposalGraphRunExecutor):
            raise RuntimeError(
                "Impact v2 binding has no ImpactProposalGraphRunExecutor registration"
            )
        async with self._orchestration_worker.lease_heartbeat(claimed.claim):
            return await claimed.compiled_graph.execute_claim(run=claimed.run, claim=claimed.claim)


__all__ = [
    "ImpactProposalGraphRunExecutor",
    "ImpactProposalLangGraphWorker",
    "ImpactProposalRunExecution",
]
