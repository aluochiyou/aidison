"""PostgreSQL control ledger for LangGraph AgentRun physical-call budgets."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import (
    AgentRunBudgetAccountRow,
    AgentRunBudgetOperationRow,
    AgentRunRow,
)
from aidison.runtime.agent_run_budget import (
    AgentRunBudgetAccount,
    AgentRunBudgetOperation,
    AgentRunBudgetOperationKind,
    AgentRunBudgetState,
)
from aidison.runtime.agent_runs import AgentRunClaim, AgentRunStatus, utc_now


class AgentRunBudgetError(RuntimeError):
    """Base error for new-runtime budget control."""


class AgentRunBudgetConflictError(AgentRunBudgetError):
    """Raised for stale ownership, invalid transitions, or divergent retries."""


class AgentRunBudgetLimitExceededError(AgentRunBudgetError):
    """Raised before a new physical call would exceed its AgentRun budget."""


class AgentRunBudgetLedger:
    """Reserve before dispatch; settle physical effects even after a takeover.

    ``reserve`` and ``mark_dispatched`` require a live AgentRun claim, so a
    stale worker cannot begin a new billable call.  ``settle`` deliberately
    does not: the call may have crossed the provider boundary before its lease
    expired, and losing that cost record would make the budget untrustworthy.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_account(
        self,
        *,
        agent_run_id: UUID,
        token_cap: int,
        tool_call_cap: int,
    ) -> AgentRunBudgetAccount:
        if token_cap < 0 or tool_call_cap < 0:
            raise ValueError("AgentRun budget caps cannot be negative")
        if await self._session.get(AgentRunRow, agent_run_id) is None:
            raise AgentRunBudgetConflictError("AgentRun does not exist")
        existing = await self._session.scalar(
            select(AgentRunBudgetAccountRow)
            .where(AgentRunBudgetAccountRow.agent_run_id == agent_run_id)
            .with_for_update()
        )
        if existing is not None:
            if existing.token_cap != token_cap or existing.tool_call_cap != tool_call_cap:
                raise AgentRunBudgetConflictError("AgentRun already has another budget cap")
            return _account(existing)
        row = AgentRunBudgetAccountRow(
            id=uuid4(),
            agent_run_id=agent_run_id,
            token_cap=token_cap,
            tool_call_cap=tool_call_cap,
            token_reserved=0,
            tool_calls_reserved=0,
            token_consumed=0,
            tool_calls_consumed=0,
            state=AgentRunBudgetState.OPEN.value,
        )
        self._session.add(row)
        await self._session.flush()
        return _account(row)

    async def get_account(self, account_id: UUID) -> AgentRunBudgetAccount:
        row = await self._session.get(AgentRunBudgetAccountRow, account_id)
        if row is None:
            raise AgentRunBudgetConflictError("AgentRun budget account not found")
        return _account(row)

    async def get_account_for_run(self, agent_run_id: UUID) -> AgentRunBudgetAccount:
        """Return the single budget authority attached to an AgentRun."""

        row = await self._session.scalar(
            select(AgentRunBudgetAccountRow).where(
                AgentRunBudgetAccountRow.agent_run_id == agent_run_id
            )
        )
        if row is None:
            raise AgentRunBudgetConflictError("AgentRun budget account not found")
        return _account(row)

    async def reserve(
        self,
        *,
        account_id: UUID,
        claim: AgentRunClaim,
        kind: AgentRunBudgetOperationKind,
        logical_step: str,
        physical_attempt_no: int,
        idempotency_key: str,
        request_hash: str,
        provider: str,
        target: str,
        reserved_tokens: int = 0,
        reserved_tool_calls: int = 0,
        request_artifact_ref: str | None = None,
    ) -> AgentRunBudgetOperation:
        self._validate_reservation(
            kind=kind,
            reserved_tokens=reserved_tokens,
            reserved_tool_calls=reserved_tool_calls,
        )
        await self._locked_active_run(claim)
        account = await self._locked_account(account_id)
        if account.agent_run_id != claim.run_id:
            raise AgentRunBudgetConflictError("budget account belongs to another AgentRun")
        existing = await self._session.scalar(
            select(AgentRunBudgetOperationRow)
            .where(AgentRunBudgetOperationRow.idempotency_key == idempotency_key)
            .with_for_update()
        )
        if existing is not None:
            if not self._same_reservation(
                existing,
                account_id=account_id,
                claim=claim,
                kind=kind,
                logical_step=logical_step,
                physical_attempt_no=physical_attempt_no,
                request_hash=request_hash,
                provider=provider,
                target=target,
                reserved_tokens=reserved_tokens,
                reserved_tool_calls=reserved_tool_calls,
                request_artifact_ref=request_artifact_ref,
            ):
                raise AgentRunBudgetConflictError(
                    "budget operation idempotency key has different content"
                )
            return _operation(existing)
        if account.state != AgentRunBudgetState.OPEN.value:
            raise AgentRunBudgetConflictError("AgentRun budget account is not open")
        if account.token_reserved + account.token_consumed + reserved_tokens > account.token_cap:
            raise AgentRunBudgetLimitExceededError("AgentRun token budget is exhausted")
        if (
            account.tool_calls_reserved + account.tool_calls_consumed + reserved_tool_calls
            > account.tool_call_cap
        ):
            raise AgentRunBudgetLimitExceededError("AgentRun tool-call budget is exhausted")
        account.token_reserved += reserved_tokens
        account.tool_calls_reserved += reserved_tool_calls
        row = AgentRunBudgetOperationRow(
            id=uuid4(),
            account_id=account_id,
            claim_generation=claim.generation,
            lease_token=claim.lease_token,
            kind=kind.value,
            logical_step=logical_step,
            physical_attempt_no=physical_attempt_no,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            provider=provider,
            target=target,
            state=AgentRunBudgetState.RESERVED.value,
            reserved_tokens=reserved_tokens,
            reserved_tool_calls=reserved_tool_calls,
            consumed_tokens=0,
            consumed_tool_calls=0,
            request_artifact_ref=request_artifact_ref,
        )
        self._session.add(row)
        await self._session.flush()
        return _operation(row)

    async def mark_dispatched(
        self,
        *,
        operation_id: UUID,
        claim: AgentRunClaim,
    ) -> AgentRunBudgetOperation:
        operation_hint = await self._session.get(AgentRunBudgetOperationRow, operation_id)
        if operation_hint is None:
            raise AgentRunBudgetConflictError("AgentRun budget operation not found")
        account = await self._session.get(AgentRunBudgetAccountRow, operation_hint.account_id)
        if account is None or account.agent_run_id != claim.run_id:
            raise AgentRunBudgetConflictError("budget operation belongs to another AgentRun")
        await self._locked_active_run(claim)
        operation = await self._locked_operation(operation_id)
        if (
            operation.claim_generation != claim.generation
            or operation.lease_token != claim.lease_token
        ):
            raise AgentRunBudgetConflictError("claim cannot dispatch another generation operation")
        if operation.state == AgentRunBudgetState.DISPATCHED.value:
            return _operation(operation)
        if operation.state != AgentRunBudgetState.RESERVED.value:
            raise AgentRunBudgetConflictError("only reserved budget operation can be dispatched")
        operation.state = AgentRunBudgetState.DISPATCHED.value
        operation.dispatched_at = utc_now()
        await self._session.flush()
        return _operation(operation)

    async def settle(
        self,
        *,
        operation_id: UUID,
        consumed_tokens: int,
        consumed_tool_calls: int,
        provider_request_id: str | None = None,
        response_artifact_ref: str | None = None,
    ) -> AgentRunBudgetOperation:
        if consumed_tokens < 0 or consumed_tool_calls < 0:
            raise ValueError("consumed budget cannot be negative")
        operation_hint = await self._session.get(AgentRunBudgetOperationRow, operation_id)
        if operation_hint is None:
            raise AgentRunBudgetConflictError("AgentRun budget operation not found")
        account = await self._locked_account(operation_hint.account_id)
        operation = await self._locked_operation(operation_id)
        if operation.state == AgentRunBudgetState.SETTLED.value:
            if not self._same_settlement(
                operation,
                consumed_tokens=consumed_tokens,
                consumed_tool_calls=consumed_tool_calls,
                provider_request_id=provider_request_id,
                response_artifact_ref=response_artifact_ref,
            ):
                raise AgentRunBudgetConflictError("budget operation already has another settlement")
            return _operation(operation)
        if operation.state != AgentRunBudgetState.DISPATCHED.value:
            raise AgentRunBudgetConflictError("only dispatched budget operation can settle")
        if consumed_tokens > operation.reserved_tokens:
            raise AgentRunBudgetConflictError("provider token usage exceeds reservation")
        if consumed_tool_calls > operation.reserved_tool_calls:
            raise AgentRunBudgetConflictError("provider tool usage exceeds reservation")
        account.token_reserved -= operation.reserved_tokens
        account.tool_calls_reserved -= operation.reserved_tool_calls
        account.token_consumed += consumed_tokens
        account.tool_calls_consumed += consumed_tool_calls
        operation.state = AgentRunBudgetState.SETTLED.value
        operation.consumed_tokens = consumed_tokens
        operation.consumed_tool_calls = consumed_tool_calls
        operation.provider_request_id = provider_request_id
        operation.response_artifact_ref = response_artifact_ref
        operation.settled_at = utc_now()
        await self._session.flush()
        return _operation(operation)

    async def release_undispatched(
        self,
        *,
        operation_id: UUID,
        claim: AgentRunClaim,
        normalized_error: str,
    ) -> AgentRunBudgetOperation:
        """Release a reservation only while no physical call was dispatched."""

        operation_hint = await self._session.get(AgentRunBudgetOperationRow, operation_id)
        if operation_hint is None:
            raise AgentRunBudgetConflictError("AgentRun budget operation not found")
        account = await self._locked_account(operation_hint.account_id)
        if account.agent_run_id != claim.run_id:
            raise AgentRunBudgetConflictError("budget operation belongs to another AgentRun")
        await self._locked_active_run(claim)
        operation = await self._locked_operation(operation_id)
        if (
            operation.claim_generation != claim.generation
            or operation.lease_token != claim.lease_token
        ):
            raise AgentRunBudgetConflictError(
                "stale claim cannot release another generation operation"
            )
        if operation.state == AgentRunBudgetState.RELEASED.value:
            return _operation(operation)
        if operation.state != AgentRunBudgetState.RESERVED.value:
            raise AgentRunBudgetConflictError("only undispatched operation can be released")
        account.token_reserved -= operation.reserved_tokens
        account.tool_calls_reserved -= operation.reserved_tool_calls
        operation.state = AgentRunBudgetState.RELEASED.value
        operation.normalized_error = normalized_error
        operation.settled_at = utc_now()
        await self._session.flush()
        return _operation(operation)

    async def mark_ambiguous(
        self,
        *,
        operation_id: UUID,
        normalized_error: str,
    ) -> AgentRunBudgetOperation:
        """Conservatively retain a sent reservation whose actual usage is unknown."""

        operation = await self._locked_operation(operation_id)
        if operation.state == AgentRunBudgetState.AMBIGUOUS.value:
            return _operation(operation)
        if operation.state != AgentRunBudgetState.DISPATCHED.value:
            raise AgentRunBudgetConflictError("only dispatched operation can become ambiguous")
        operation.state = AgentRunBudgetState.AMBIGUOUS.value
        operation.normalized_error = normalized_error
        operation.settled_at = utc_now()
        await self._session.flush()
        return _operation(operation)

    async def _locked_account(self, account_id: UUID) -> AgentRunBudgetAccountRow:
        row = await self._session.scalar(
            select(AgentRunBudgetAccountRow)
            .where(AgentRunBudgetAccountRow.id == account_id)
            .with_for_update()
        )
        if row is None:
            raise AgentRunBudgetConflictError("AgentRun budget account not found")
        return row

    async def _locked_operation(self, operation_id: UUID) -> AgentRunBudgetOperationRow:
        row = await self._session.scalar(
            select(AgentRunBudgetOperationRow)
            .where(AgentRunBudgetOperationRow.id == operation_id)
            .with_for_update()
        )
        if row is None:
            raise AgentRunBudgetConflictError("AgentRun budget operation not found")
        return row

    async def _locked_active_run(self, claim: AgentRunClaim) -> AgentRunRow:
        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == claim.run_id).with_for_update()
        )
        if row is None:
            raise AgentRunBudgetConflictError("AgentRun does not exist")
        if (
            row.status != AgentRunStatus.RUNNING.value
            or row.current_generation != claim.generation
            or row.lease_token != claim.lease_token
            or row.lease_expires_at is None
            or row.lease_expires_at <= datetime.now(UTC)
        ):
            raise AgentRunBudgetConflictError("stale AgentRun claim cannot mutate budget control")
        return row

    @staticmethod
    def _validate_reservation(
        *,
        kind: AgentRunBudgetOperationKind,
        reserved_tokens: int,
        reserved_tool_calls: int,
    ) -> None:
        if reserved_tokens < 0 or reserved_tool_calls < 0:
            raise ValueError("reserved budget cannot be negative")
        if kind is AgentRunBudgetOperationKind.MODEL:
            if reserved_tokens < 1 or reserved_tool_calls != 0:
                raise ValueError("model operation must reserve tokens and no tool calls")
        elif reserved_tokens != 0 or reserved_tool_calls != 1:
            raise ValueError("tool operation must reserve one tool call and no tokens")

    @staticmethod
    def _same_reservation(
        row: AgentRunBudgetOperationRow,
        *,
        account_id: UUID,
        claim: AgentRunClaim,
        kind: AgentRunBudgetOperationKind,
        logical_step: str,
        physical_attempt_no: int,
        request_hash: str,
        provider: str,
        target: str,
        reserved_tokens: int,
        reserved_tool_calls: int,
        request_artifact_ref: str | None,
    ) -> bool:
        return (
            row.account_id == account_id
            and row.claim_generation == claim.generation
            and row.lease_token == claim.lease_token
            and row.kind == kind.value
            and row.logical_step == logical_step
            and row.physical_attempt_no == physical_attempt_no
            and row.request_hash == request_hash
            and row.provider == provider
            and row.target == target
            and row.reserved_tokens == reserved_tokens
            and row.reserved_tool_calls == reserved_tool_calls
            and row.request_artifact_ref == request_artifact_ref
        )

    @staticmethod
    def _same_settlement(
        row: AgentRunBudgetOperationRow,
        *,
        consumed_tokens: int,
        consumed_tool_calls: int,
        provider_request_id: str | None,
        response_artifact_ref: str | None,
    ) -> bool:
        return (
            row.consumed_tokens == consumed_tokens
            and row.consumed_tool_calls == consumed_tool_calls
            and row.provider_request_id == provider_request_id
            and row.response_artifact_ref == response_artifact_ref
        )


def _account(row: AgentRunBudgetAccountRow) -> AgentRunBudgetAccount:
    return AgentRunBudgetAccount(
        id=row.id,
        agent_run_id=row.agent_run_id,
        token_cap=row.token_cap,
        tool_call_cap=row.tool_call_cap,
        token_reserved=row.token_reserved,
        tool_calls_reserved=row.tool_calls_reserved,
        token_consumed=row.token_consumed,
        tool_calls_consumed=row.tool_calls_consumed,
        state=AgentRunBudgetState(row.state),
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


def _operation(row: AgentRunBudgetOperationRow) -> AgentRunBudgetOperation:
    return AgentRunBudgetOperation(
        id=row.id,
        account_id=row.account_id,
        claim_generation=row.claim_generation,
        lease_token=row.lease_token,
        kind=AgentRunBudgetOperationKind(row.kind),
        logical_step=row.logical_step,
        physical_attempt_no=row.physical_attempt_no,
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        provider=row.provider,
        target=row.target,
        state=AgentRunBudgetState(row.state),
        reserved_tokens=row.reserved_tokens,
        reserved_tool_calls=row.reserved_tool_calls,
        consumed_tokens=row.consumed_tokens,
        consumed_tool_calls=row.consumed_tool_calls,
        provider_request_id=row.provider_request_id,
        request_artifact_ref=row.request_artifact_ref,
        response_artifact_ref=row.response_artifact_ref,
        normalized_error=row.normalized_error,
        created_at=row.created_at,
        dispatched_at=row.dispatched_at,
        settled_at=row.settled_at,
    )


__all__ = [
    "AgentRunBudgetConflictError",
    "AgentRunBudgetError",
    "AgentRunBudgetLedger",
    "AgentRunBudgetLimitExceededError",
]
