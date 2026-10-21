from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver

from aidison.application.agent_run_application import DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING
from aidison.application.impact_proposal_run_execution import (
    ImpactProposalGraphRunExecutor,
    ImpactProposalLangGraphWorker,
    ImpactProposalRunExecution,
)
from aidison.application.langgraph_worker import ClaimedGraphRun
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim, AgentRunKind


class _OrchestrationWorkerFixture:
    def __init__(self, claimed: ClaimedGraphRun) -> None:
        self._claimed = claimed
        self.heartbeat_claims: list[AgentRunClaim] = []

    async def claim_once(self) -> ClaimedGraphRun:
        return self._claimed

    @asynccontextmanager
    async def lease_heartbeat(self, claim: AgentRunClaim):
        self.heartbeat_claims.append(claim)
        yield


def test_impact_worker_keeps_its_run_lease_while_executing(tmp_path: Path) -> None:
    run = AgentRun(
        project_id=UUID(int=1),
        kind=AgentRunKind.IMPACT,
        idempotency_key="impact-worker-heartbeat",
        basis_hash="a" * 64,
        basis_project_revision=1,
        runtime_binding=DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING,
        thread_id="agent-run:impact-worker-heartbeat",
    )
    claim = AgentRunClaim(
        run_id=run.id,
        worker_id="impact-worker",
        generation=1,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    executor = ImpactProposalGraphRunExecutor(
        session_factory=object(),
        artifact_root=tmp_path,
        checkpointer=InMemorySaver(),
        analyst=object(),
    )
    executed: list[AgentRunClaim] = []

    async def execute_claim(*, run: AgentRun, claim: AgentRunClaim) -> ImpactProposalRunExecution:
        assert run.id == claim.run_id
        executed.append(claim)
        return ImpactProposalRunExecution(readiness="ready", agent_decision=None)

    executor.execute_claim = execute_claim  # type: ignore[method-assign]
    orchestration_worker = _OrchestrationWorkerFixture(
        ClaimedGraphRun(run=run, claim=claim, compiled_graph=executor)
    )

    result = asyncio.run(
        ImpactProposalLangGraphWorker(  # type: ignore[arg-type]
            orchestration_worker=orchestration_worker
        ).run_once()
    )

    assert result == ImpactProposalRunExecution(readiness="ready", agent_decision=None)
    assert orchestration_worker.heartbeat_claims == [claim]
    assert executed == [claim]
