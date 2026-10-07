"""Turn a READY ImpactGraph interruption into exactly one durable user decision."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.impact.proposal import ImpactProposalManifest
from aidison.infrastructure.agent_decisions import AgentRunDecisionStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import session_scope
from aidison.research.decision_contracts import AgentRunDecision
from aidison.research.langgraph_contracts import ProposalManifest
from aidison.runtime.agent_runs import AdmittedCheckpointRef, AgentRun, AgentRunClaim
from aidison.runtime.minimal_graph import admitted_checkpoint_from_snapshot, thread_config


class ImpactDecisionBridge:
    """Durably admit the exact impact review checkpoint before waiting for the user."""

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
        compiled = cast(Any, graph)
        snapshot = await compiled.aget_state(thread_config(thread_id=run.thread_id))
        if not snapshot.interrupts:
            raise RuntimeError("ImpactGraph did not stop for user decision")
        values = cast(dict[str, Any], snapshot.values)
        proposal_ref = values.get("proposal_manifest_ref")
        if not isinstance(proposal_ref, str) or values.get("proposal_readiness") != "ready":
            raise RuntimeError("ImpactGraph interrupt is not a READY impact proposal")
        manifest_hash, _ = ContentAddressedArtifactStore.parse_ref(proposal_ref)
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            manifest = ImpactProposalManifest.model_validate(
                await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=proposal_ref,
                    expected_kind="impact_proposal_manifest",
                )
            )
            if (
                manifest.run_id != run.id
                or manifest.project_id != run.project_id
                or manifest.basis_hash != run.basis_hash
                or not manifest.can_create_patch
            ):
                raise RuntimeError("Impact Proposal Manifest is not eligible for user decision")
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


__all__ = ["ImpactDecisionBridge"]
