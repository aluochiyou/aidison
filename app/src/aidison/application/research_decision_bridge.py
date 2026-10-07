"""Turn a ResearchGraph interrupt into a durable user-decision inbox item."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_decisions import AgentRunDecisionStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import session_scope
from aidison.research.decision_contracts import AgentRunDecision
from aidison.research.langgraph_contracts import ProposalManifest
from aidison.runtime.agent_runs import AdmittedCheckpointRef, AgentRun, AgentRunClaim
from aidison.runtime.minimal_graph import admitted_checkpoint_from_snapshot, thread_config


class ResearchDecisionBridge:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def prepare_from_interrupt(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        graph: object,
        proposal_override: ProposalManifest | None = None,
    ) -> tuple[AgentRunDecision, AdmittedCheckpointRef]:
        compiled = cast(Any, graph)
        snapshot = await compiled.aget_state(thread_config(thread_id=run.thread_id))
        if not snapshot.interrupts:
            raise RuntimeError("ResearchGraph did not stop for user decision")
        values = cast(dict[str, Any], snapshot.values)
        if proposal_override is not None:
            if proposal_override.run_id != run.id or proposal_override.basis_hash != run.basis_hash:
                raise ValueError("proposal override does not match the pinned Research AgentRun")
            # ``aupdate_state`` after an interrupt consumes that interrupt in
            # current LangGraph. The admitted checkpoint must therefore remain
            # the original interrupt anchor; the durable Decision is the
            # business authority for the reviewed, aggregate Manifest.
            proposal = proposal_override
        else:
            proposal = ProposalManifest(
                run_id=run.id,
                basis_hash=run.basis_hash,
                artifact_ref=values["proposal_manifest_ref"],
                manifest_hash=values["proposal_manifest_hash"],
            )
        checkpoint = admitted_checkpoint_from_snapshot(
            snapshot=snapshot,
            binding=run.runtime_binding,
            generation=claim.generation,
        )
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            await control.admit_checkpoint(claim=claim, checkpoint=checkpoint)
            decision = await AgentRunDecisionStore(session).prepare(run=run, proposal=proposal)
            await control.wait_for_decision(claim=claim)
        return decision, checkpoint
