"""Turn a READY SolutionGraph interrupt into one durable human decision."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_decisions import AgentRunDecisionStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import session_scope
from aidison.research.decision_contracts import AgentRunDecision
from aidison.research.langgraph_contracts import ProposalManifest
from aidison.runtime.agent_runs import AdmittedCheckpointRef, AgentRun, AgentRunClaim
from aidison.runtime.minimal_graph import admitted_checkpoint_from_snapshot, execution_thread_config
from aidison.solution.proposal_manifest import (
    SolutionProposalManifest,
    SolutionProposalReadiness,
)


class SolutionDecisionBridge:
    """Admit the exact interrupt checkpoint before making the review item durable."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root

    async def prepare_from_interrupt(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        graph: object,
    ) -> tuple[AgentRunDecision, AdmittedCheckpointRef]:
        """Reject non-ready or non-interrupted graphs before Control state changes."""

        compiled = cast(Any, graph)
        snapshot = await compiled.aget_state(
            execution_thread_config(thread_id=run.thread_id, generation=claim.generation)
        )
        if not snapshot.interrupts:
            raise RuntimeError("SolutionGraph did not stop for user decision")
        values = cast(dict[str, Any], snapshot.values)
        proposal_ref = values.get("proposal_manifest_ref")
        readiness = values.get("proposal_readiness")
        if not isinstance(proposal_ref, str) or readiness != SolutionProposalReadiness.READY.value:
            raise RuntimeError("SolutionGraph interrupt is not a READY solution proposal")
        manifest_hash, _ = ContentAddressedArtifactStore.parse_ref(proposal_ref)
        async with session_scope(self._session_factory) as session:
            artifact = ContentAddressedArtifactStore(session, self._artifact_root)
            payload = await artifact.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=proposal_ref,
                expected_kind="solution_proposal_manifest",
            )
            manifest = SolutionProposalManifest.model_validate(payload)
            if (
                manifest.run_id != run.id
                or manifest.project_id != run.project_id
                or manifest.basis_hash != run.basis_hash
                or manifest.readiness is not SolutionProposalReadiness.READY
            ):
                raise RuntimeError("Solution Proposal Manifest is not eligible for user decision")
            checkpoint = admitted_checkpoint_from_snapshot(
                snapshot=snapshot,
                binding=run.runtime_binding,
                generation=claim.generation,
            )
            control = AgentRunControl(session)
            await control.admit_checkpoint(claim=claim, checkpoint=checkpoint)
            decision = await AgentRunDecisionStore(session).prepare(
                run=run,
                proposal=ProposalManifest(
                    run_id=run.id,
                    basis_hash=run.basis_hash,
                    artifact_ref=proposal_ref,
                    manifest_hash=manifest_hash,
                ),
            )
            await control.wait_for_decision(claim=claim)
        return decision, checkpoint


__all__ = ["SolutionDecisionBridge"]
