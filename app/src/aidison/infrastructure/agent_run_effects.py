"""PostgreSQL effect ledger for authorized external writes in an AgentRun."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import AgentRunEffectRow, AgentRunRow
from aidison.runtime.agent_run_effect_events import (
    AgentRunEffectEventType,
    agent_run_effect_event_metadata,
)
from aidison.runtime.agent_run_effects import (
    AgentRunEffect,
    AgentRunEffectIntent,
    AgentRunEffectState,
    EffectReconciliationOutcome,
)
from aidison.runtime.agent_runs import AgentRunClaim, AgentRunStatus, utc_now


class AgentRunEffectError(RuntimeError):
    pass


class AgentRunEffectConflictError(AgentRunEffectError):
    pass


class AgentRunEffectLedger:
    """Durably prevent blind redelivery of an uncertain external write."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, effect_id: UUID) -> AgentRunEffect | None:
        """Read one effect ledger row without inferring whether it is safe to retry."""
        row = await self._session.get(AgentRunEffectRow, effect_id)
        return _effect(row) if row is not None else None

    async def prepare(
        self, *, intent: AgentRunEffectIntent, claim: AgentRunClaim
    ) -> AgentRunEffect:
        if intent.run_id != claim.run_id:
            raise AgentRunEffectConflictError("effect intent belongs to another AgentRun")
        run = await self._locked_active_run(claim)
        existing = await self._session.scalar(
            select(AgentRunEffectRow)
            .where(AgentRunEffectRow.idempotency_key == intent.idempotency_key)
            .with_for_update()
        )
        if existing is not None:
            if not self._same_intent(existing, intent):
                raise AgentRunEffectConflictError("effect idempotency key has different content")
            return _effect(existing)
        row = AgentRunEffectRow(
            id=uuid4(),
            agent_run_id=intent.run_id,
            task_id=intent.task_id,
            basis_hash=intent.basis_hash,
            approval_ref=intent.approval_ref,
            effect_kind=intent.effect_kind,
            provider=intent.provider,
            external_idempotency_key=intent.external_idempotency_key,
            idempotency_key=intent.idempotency_key,
            request_hash=intent.request_hash,
            request_artifact_ref=intent.request_artifact_ref,
            claim_generation=claim.generation,
            lease_token=claim.lease_token,
            state=AgentRunEffectState.PREPARED.value,
            event_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        await self._append_event(
            row=row,
            project_id=run.project_id,
            event_type=AgentRunEffectEventType.PREPARED,
            occurred_at=row.created_at,
        )
        return _effect(row)

    async def mark_dispatched(self, *, effect_id: UUID, claim: AgentRunClaim) -> AgentRunEffect:
        effect_hint = await self._session.get(AgentRunEffectRow, effect_id)
        if effect_hint is None:
            raise AgentRunEffectConflictError("AgentRun effect not found")
        if effect_hint.agent_run_id != claim.run_id:
            raise AgentRunEffectConflictError("effect belongs to another AgentRun")
        run = await self._locked_active_run(claim)
        row = await self._locked_effect(effect_id)
        if row.claim_generation != claim.generation or row.lease_token != claim.lease_token:
            raise AgentRunEffectConflictError("stale claim cannot dispatch external effect")
        if row.state == AgentRunEffectState.DISPATCHED.value:
            return _effect(row)
        if row.state != AgentRunEffectState.PREPARED.value:
            raise AgentRunEffectConflictError("only prepared effect can be dispatched")
        row.state = AgentRunEffectState.DISPATCHED.value
        row.dispatched_at = utc_now()
        self._advance_event_version(row)
        await self._session.flush()
        await self._append_event(
            row=row,
            project_id=run.project_id,
            event_type=AgentRunEffectEventType.DISPATCHED,
            occurred_at=row.dispatched_at,
        )
        return _effect(row)

    async def mark_succeeded(
        self,
        *,
        effect_id: UUID,
        provider_effect_id: str | None,
        response_artifact_ref: str,
    ) -> AgentRunEffect:
        row = await self._locked_effect(effect_id)
        if row.state == AgentRunEffectState.SUCCEEDED.value:
            if (
                row.provider_effect_id != provider_effect_id
                or row.response_artifact_ref != response_artifact_ref
            ):
                raise AgentRunEffectConflictError("effect already has another success outcome")
            return _effect(row)
        if row.state != AgentRunEffectState.DISPATCHED.value:
            raise AgentRunEffectConflictError("only dispatched effect can succeed")
        row.state = AgentRunEffectState.SUCCEEDED.value
        row.provider_effect_id = provider_effect_id
        row.response_artifact_ref = response_artifact_ref
        row.resolved_at = utc_now()
        self._advance_event_version(row)
        await self._session.flush()
        await self._append_event(
            row=row,
            project_id=await self._project_id(row.agent_run_id),
            event_type=AgentRunEffectEventType.SUCCEEDED,
            occurred_at=row.resolved_at,
        )
        return _effect(row)

    async def mark_failed(
        self, *, effect_id: UUID, failure_ref: str, normalized_error: str
    ) -> AgentRunEffect:
        row = await self._locked_effect(effect_id)
        if row.state == AgentRunEffectState.FAILED.value:
            if row.failure_ref != failure_ref or row.normalized_error != normalized_error:
                raise AgentRunEffectConflictError("effect already has another failure outcome")
            return _effect(row)
        if row.state not in {
            AgentRunEffectState.PREPARED.value,
            AgentRunEffectState.DISPATCHED.value,
        }:
            raise AgentRunEffectConflictError("only unresolved effect can fail")
        row.state = AgentRunEffectState.FAILED.value
        row.failure_ref = failure_ref
        row.normalized_error = normalized_error
        row.resolved_at = utc_now()
        self._advance_event_version(row)
        await self._session.flush()
        await self._append_event(
            row=row,
            project_id=await self._project_id(row.agent_run_id),
            event_type=AgentRunEffectEventType.FAILED,
            occurred_at=row.resolved_at,
        )
        return _effect(row)

    async def mark_ambiguous(self, *, effect_id: UUID, normalized_error: str) -> AgentRunEffect:
        row = await self._locked_effect(effect_id)
        if row.state == AgentRunEffectState.AMBIGUOUS.value:
            if row.normalized_error != normalized_error:
                raise AgentRunEffectConflictError("effect already has another ambiguity reason")
            return _effect(row)
        if row.state != AgentRunEffectState.DISPATCHED.value:
            raise AgentRunEffectConflictError("only dispatched effect can become ambiguous")
        row.state = AgentRunEffectState.AMBIGUOUS.value
        row.normalized_error = normalized_error
        row.resolved_at = utc_now()
        self._advance_event_version(row)
        await self._session.flush()
        await self._append_event(
            row=row,
            project_id=await self._project_id(row.agent_run_id),
            event_type=AgentRunEffectEventType.AMBIGUOUS,
            occurred_at=row.resolved_at,
        )
        return _effect(row)

    async def reconcile(
        self,
        *,
        effect_id: UUID,
        outcome: EffectReconciliationOutcome,
        reconciliation_artifact_ref: str,
    ) -> AgentRunEffect:
        row = await self._locked_effect(effect_id)
        if row.state != AgentRunEffectState.AMBIGUOUS.value:
            raise AgentRunEffectConflictError("only ambiguous effect can reconcile")
        row.state = AgentRunEffectState(outcome).value
        row.reconciliation_artifact_ref = reconciliation_artifact_ref
        row.reconciled_at = utc_now()
        self._advance_event_version(row)
        await self._session.flush()
        await self._append_event(
            row=row,
            project_id=await self._project_id(row.agent_run_id),
            event_type={
                EffectReconciliationOutcome.SUCCEEDED: AgentRunEffectEventType.RECONCILED_SUCCEEDED,
                EffectReconciliationOutcome.FAILED: AgentRunEffectEventType.RECONCILED_FAILED,
            }[outcome],
            occurred_at=row.reconciled_at,
        )
        return _effect(row)

    @staticmethod
    def _advance_event_version(row: AgentRunEffectRow) -> None:
        """Legacy rows keep relational semantics; new rows advance atomically."""

        if row.event_version > 0:
            row.event_version += 1

    async def _append_event(
        self,
        *,
        row: AgentRunEffectRow,
        project_id: UUID,
        event_type: AgentRunEffectEventType,
        occurred_at: datetime | None,
    ) -> None:
        if row.event_version == 0:
            return
        if occurred_at is None:
            raise AgentRunEffectConflictError("effect event timestamp is missing")
        from aidison.infrastructure.store import PostgresDomainStore

        effect = _effect(row)
        payload: dict[str, object] = {"effect": effect.model_dump(mode="json")}
        metadata = agent_run_effect_event_metadata(
            effect=effect,
            aggregate_version=row.event_version,
            payload=payload,
            occurred_at=occurred_at,
            event_type=event_type,
        )
        await PostgresDomainStore(self._session).append_event(
            project_id,
            event_type.value,
            payload,
            metadata,
        )

    async def _project_id(self, run_id: UUID) -> UUID:
        project_id = await self._session.scalar(
            select(AgentRunRow.project_id).where(AgentRunRow.id == run_id)
        )
        if project_id is None:
            raise AgentRunEffectConflictError("AgentRun does not exist")
        return project_id

    async def _locked_effect(self, effect_id: UUID) -> AgentRunEffectRow:
        row = await self._session.scalar(
            select(AgentRunEffectRow).where(AgentRunEffectRow.id == effect_id).with_for_update()
        )
        if row is None:
            raise AgentRunEffectConflictError("AgentRun effect not found")
        return row

    async def _locked_active_run(self, claim: AgentRunClaim) -> AgentRunRow:
        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == claim.run_id).with_for_update()
        )
        if row is None:
            raise AgentRunEffectConflictError("AgentRun does not exist")
        if (
            row.status != AgentRunStatus.RUNNING.value
            or row.current_generation != claim.generation
            or row.lease_token != claim.lease_token
            or row.lease_expires_at is None
            or row.lease_expires_at <= datetime.now(UTC)
        ):
            raise AgentRunEffectConflictError("stale AgentRun claim cannot mutate effect control")
        return row

    @staticmethod
    def _same_intent(row: AgentRunEffectRow, intent: AgentRunEffectIntent) -> bool:
        return (
            row.agent_run_id == intent.run_id
            and row.task_id == intent.task_id
            and row.basis_hash == intent.basis_hash
            and row.approval_ref == intent.approval_ref
            and row.effect_kind == intent.effect_kind
            and row.provider == intent.provider
            and row.external_idempotency_key == intent.external_idempotency_key
            and row.request_hash == intent.request_hash
            and row.request_artifact_ref == intent.request_artifact_ref
        )


def _effect(row: AgentRunEffectRow) -> AgentRunEffect:
    return AgentRunEffect(
        id=row.id,
        intent=AgentRunEffectIntent(
            run_id=row.agent_run_id,
            task_id=row.task_id,
            basis_hash=row.basis_hash,
            approval_ref=row.approval_ref,
            effect_kind=row.effect_kind,
            provider=row.provider,
            external_idempotency_key=row.external_idempotency_key,
            idempotency_key=row.idempotency_key,
            request_hash=row.request_hash,
            request_artifact_ref=row.request_artifact_ref,
        ),
        claim_generation=row.claim_generation,
        lease_token=row.lease_token,
        state=AgentRunEffectState(row.state),
        provider_effect_id=row.provider_effect_id,
        response_artifact_ref=row.response_artifact_ref,
        failure_ref=row.failure_ref,
        normalized_error=row.normalized_error,
        reconciliation_artifact_ref=row.reconciliation_artifact_ref,
        created_at=row.created_at,
        dispatched_at=row.dispatched_at,
        resolved_at=row.resolved_at,
        reconciled_at=row.reconciled_at,
    )


__all__ = ["AgentRunEffectConflictError", "AgentRunEffectError", "AgentRunEffectLedger"]
