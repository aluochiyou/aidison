"""Persistence for non-canonical ProposalManifest user review."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import AgentRunDecisionRow
from aidison.research.decision_contracts import AgentRunDecision, AgentRunDecisionStatus
from aidison.research.langgraph_contracts import ProposalManifest
from aidison.runtime.agent_run_decision_events import (
    AgentRunDecisionEventType,
    agent_run_decision_event_metadata,
)
from aidison.runtime.agent_runs import AgentRun


class AgentRunDecisionConflictError(RuntimeError):
    pass


class AgentRunDecisionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prepare(self, *, run: AgentRun, proposal: ProposalManifest) -> AgentRunDecision:
        if proposal.run_id != run.id or proposal.basis_hash != run.basis_hash:
            raise AgentRunDecisionConflictError("ProposalManifest does not belong to the AgentRun")
        existing = await self._session.scalar(
            select(AgentRunDecisionRow)
            .where(AgentRunDecisionRow.agent_run_id == run.id)
            .with_for_update()
        )
        if existing is not None:
            restored = self._from_row(existing)
            if not self._matches_proposal(restored, proposal):
                raise AgentRunDecisionConflictError(
                    "AgentRun already has another proposal decision"
                )
            return restored
        decision = AgentRunDecision(
            agent_run_id=run.id,
            project_id=run.project_id,
            basis_hash=run.basis_hash,
            proposal_manifest_ref=proposal.artifact_ref,
            proposal_manifest_hash=proposal.manifest_hash,
        )
        row = AgentRunDecisionRow(
            id=decision.id,
            agent_run_id=decision.agent_run_id,
            project_id=decision.project_id,
            basis_hash=decision.basis_hash,
            proposal_manifest_ref=decision.proposal_manifest_ref,
            proposal_manifest_hash=decision.proposal_manifest_hash,
            status=decision.status.value,
            answer=None,
            event_version=1,
            created_at=decision.created_at,
        )
        try:
            # The one-decision-per-Run constraint is the durable user-review
            # boundary. Two graph resumes can both observe no row before one
            # of them commits, so isolate the insert race and return the
            # already-prepared equivalent review instead of leaking a unique
            # constraint through the application layer.
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush([row])
        except IntegrityError:
            existing = await self._session.scalar(
                select(AgentRunDecisionRow)
                .where(AgentRunDecisionRow.agent_run_id == run.id)
                .with_for_update()
            )
            if existing is None:
                raise
            restored = self._from_row(existing)
            if not self._matches_proposal(restored, proposal):
                raise AgentRunDecisionConflictError(
                    "AgentRun already has another proposal decision"
                ) from None
            return restored
        await self._append_event(
            decision=decision,
            aggregate_version=1,
            event_type=AgentRunDecisionEventType.PREPARED,
            occurred_at=row.created_at,
        )
        return decision

    async def get(self, decision_id: UUID) -> AgentRunDecision | None:
        row = await self._session.get(AgentRunDecisionRow, decision_id)
        return self._from_row(row) if row is not None else None

    async def resolve(
        self,
        *,
        decision_id: UUID,
        basis_hash: str,
        answer: AgentRunDecisionStatus,
    ) -> AgentRunDecision:
        if answer is AgentRunDecisionStatus.PENDING:
            raise ValueError("decision answer must be approved or rejected")
        row = await self._session.scalar(
            select(AgentRunDecisionRow)
            .where(AgentRunDecisionRow.id == decision_id)
            .with_for_update()
        )
        if row is None:
            raise AgentRunDecisionConflictError("AgentRun decision not found")
        decision = self._from_row(row)
        if decision.basis_hash != basis_hash:
            raise AgentRunDecisionConflictError("AgentRun decision basis is stale")
        if decision.status is not AgentRunDecisionStatus.PENDING:
            if decision.status is answer:
                return decision
            raise AgentRunDecisionConflictError("AgentRun decision is already resolved")
        resolved = decision.model_copy(
            update={"status": answer, "answer": answer.value, "resolved_at": datetime.now(UTC)}
        )
        row.status = resolved.status.value
        row.answer = resolved.answer
        row.resolved_at = resolved.resolved_at
        if row.event_version == 1:
            row.event_version = 2
        await self._session.flush()
        if row.event_version == 2:
            await self._append_event(
                decision=resolved,
                aggregate_version=2,
                event_type={
                    AgentRunDecisionStatus.APPROVED: AgentRunDecisionEventType.APPROVED,
                    AgentRunDecisionStatus.REJECTED: AgentRunDecisionEventType.REJECTED,
                }[resolved.status],
                occurred_at=resolved.resolved_at,
            )
        return resolved

    async def _append_event(
        self,
        *,
        decision: AgentRunDecision,
        aggregate_version: int,
        event_type: AgentRunDecisionEventType,
        occurred_at: datetime | None,
    ) -> None:
        if occurred_at is None:
            raise AgentRunDecisionConflictError("decision event timestamp is missing")
        from aidison.infrastructure.store import PostgresDomainStore

        payload: dict[str, object] = {"decision": decision.model_dump(mode="json")}
        metadata = agent_run_decision_event_metadata(
            decision=decision,
            aggregate_version=aggregate_version,
            payload=payload,
            occurred_at=occurred_at,
        )
        await PostgresDomainStore(self._session).append_event(
            decision.project_id,
            event_type.value,
            payload,
            metadata,
        )

    @staticmethod
    def _from_row(row: AgentRunDecisionRow) -> AgentRunDecision:
        return AgentRunDecision(
            id=row.id,
            agent_run_id=row.agent_run_id,
            project_id=row.project_id,
            basis_hash=row.basis_hash,
            proposal_manifest_ref=row.proposal_manifest_ref,
            proposal_manifest_hash=row.proposal_manifest_hash,
            status=AgentRunDecisionStatus(row.status),
            answer=row.answer,
            created_at=row.created_at,
            resolved_at=row.resolved_at,
        )

    @staticmethod
    def _matches_proposal(
        decision: AgentRunDecision,
        proposal: ProposalManifest,
    ) -> bool:
        return (
            decision.proposal_manifest_ref == proposal.artifact_ref
            and decision.proposal_manifest_hash == proposal.manifest_hash
        )
