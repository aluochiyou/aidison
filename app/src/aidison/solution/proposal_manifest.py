"""Reviewable, immutable proposal manifest for the SolutionGraph output."""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import session_scope
from aidison.research.langgraph_contracts import ProposalManifest
from aidison.runtime.agent_runs import AgentRun
from aidison.solution.contracts import SolutionContract
from aidison.solution.integration import IntegrationCheckReport, IntegrationCheckStatus

if TYPE_CHECKING:
    from aidison.solution.composition_executor import SolutionCompositionExecution


class SolutionProposalReadiness(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    NEEDS_VERIFICATION = "needs_verification"
    BLOCKED = "blocked"


class SolutionProposalManifest(BaseModel):
    """Frozen user-review input, not a canonical ``SolutionVersion`` mutation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "solution-proposal-manifest-v1"
    run_id: UUID
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_contract_ref: str = Field(min_length=1, max_length=500)
    contract_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage_contract_ref: str = Field(min_length=1, max_length=500)
    normalized_elements_ref: str = Field(min_length=1, max_length=500)
    normalized_elements_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    integration_report_ref: str = Field(min_length=1, max_length=500)
    integration_report_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted_decision_refs: tuple[str, ...] = Field(min_length=1, max_length=128)
    unresolved_refs: tuple[str, ...] = Field(max_length=64)
    readiness: SolutionProposalReadiness
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def immutable_refs_are_unique_and_content_hash_matches(self) -> SolutionProposalManifest:
        for field_name in ("accepted_decision_refs", "unresolved_refs"):
            values = getattr(self, field_name)
            if len(set(values)) != len(values):
                raise ValueError(f"SolutionProposalManifest {field_name} must be unique")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_hash = sha256(canonical.encode()).hexdigest()
        if self.content_hash is not None and self.content_hash != expected_hash:
            raise ValueError("content_hash does not match SolutionProposalManifest content")
        object.__setattr__(self, "content_hash", expected_hash)
        return self

    @property
    def can_create_solution_version(self) -> bool:
        return self.readiness is SolutionProposalReadiness.READY


class FrozenSolutionProposal(BaseModel):
    """Detailed solution review data plus the generic durable-decision handle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal: SolutionProposalManifest
    review_manifest: ProposalManifest


def derive_solution_readiness(
    *,
    integration: IntegrationCheckReport,
    unresolved_refs: tuple[str, ...],
) -> SolutionProposalReadiness:
    """Use deterministic gate output rather than model confidence or task count."""

    if integration.outcome is IntegrationCheckStatus.BLOCKED:
        return SolutionProposalReadiness.BLOCKED
    if integration.outcome is IntegrationCheckStatus.NEEDS_VERIFICATION:
        return SolutionProposalReadiness.NEEDS_VERIFICATION
    if unresolved_refs:
        return SolutionProposalReadiness.PARTIAL
    return SolutionProposalReadiness.READY


def build_solution_proposal_manifest(
    *,
    contract: SolutionContract,
    run_contract_ref: str,
    normalized_elements_ref: str,
    normalized_elements_hash: str,
    integration_report_ref: str,
    integration_report_hash: str,
    integration: IntegrationCheckReport,
    unresolved_refs: tuple[str, ...],
) -> SolutionProposalManifest:
    """Build the exact immutable bundle a future decision bridge must review."""

    if contract.content_hash is None:  # pragma: no cover - contract validator always assigns it
        raise ValueError("SolutionContract is missing its content hash")
    return SolutionProposalManifest(
        run_id=contract.solution_run_id,
        project_id=contract.project_id,
        basis_hash=contract.basis_hash,
        run_contract_ref=run_contract_ref,
        contract_content_hash=contract.content_hash,
        coverage_contract_ref=contract.coverage_contract_ref,
        normalized_elements_ref=normalized_elements_ref,
        normalized_elements_hash=normalized_elements_hash,
        integration_report_ref=integration_report_ref,
        integration_report_hash=integration_report_hash,
        accepted_decision_refs=contract.accepted_decision_refs,
        unresolved_refs=unresolved_refs,
        readiness=derive_solution_readiness(
            integration=integration,
            unresolved_refs=unresolved_refs,
        ),
    )


class SolutionProposalFreezer:
    """Persist a reviewable proposal manifest after structural result admission."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root

    async def freeze(
        self,
        *,
        run: AgentRun,
        contract: SolutionContract,
        execution: SolutionCompositionExecution,
    ) -> FrozenSolutionProposal:
        """Freeze every review input by exact immutable reference, never by live query."""

        if run.id != contract.solution_run_id or run.project_id != contract.project_id:
            raise ValueError("SolutionContract does not belong to the AgentRun")
        if run.basis_hash != contract.basis_hash:
            raise ValueError("SolutionContract does not match the AgentRun basis")
        if run.run_contract_ref is None:
            raise ValueError("Solution AgentRun has no pinned run contract Artifact")
        normalized_hash, _ = ContentAddressedArtifactStore.parse_ref(
            execution.normalized_artifact_ref
        )
        if normalized_hash != execution.result.manifest_hash:
            raise ValueError("normalized solution Artifact hash does not match ResultEnvelope")
        integration_hash, _ = ContentAddressedArtifactStore.parse_ref(
            execution.integration_artifact_ref
        )
        proposal = build_solution_proposal_manifest(
            contract=contract,
            run_contract_ref=run.run_contract_ref,
            normalized_elements_ref=execution.normalized_artifact_ref,
            normalized_elements_hash=normalized_hash,
            integration_report_ref=execution.integration_artifact_ref,
            integration_report_hash=integration_hash,
            integration=execution.integration,
            unresolved_refs=execution.result.unresolved_refs,
        )
        async with session_scope(self._session_factory) as session:
            artifact = await ContentAddressedArtifactStore(
                session, self._artifact_root
            ).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="solution_proposal_manifest",
                value=proposal.model_dump(mode="json"),
            )
        return FrozenSolutionProposal(
            proposal=proposal,
            review_manifest=ProposalManifest(
                run_id=run.id,
                basis_hash=run.basis_hash,
                artifact_ref=artifact.ref,
                manifest_hash=artifact.content_hash,
            ),
        )


__all__ = [
    "SolutionProposalManifest",
    "SolutionProposalReadiness",
    "FrozenSolutionProposal",
    "SolutionProposalFreezer",
    "build_solution_proposal_manifest",
    "derive_solution_readiness",
]
