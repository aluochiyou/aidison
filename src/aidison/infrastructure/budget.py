from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import (
    AttemptRow,
    BudgetAccountRow,
    BudgetAllocationRow,
    BudgetOperationRow,
    JobRow,
)
from aidison.runtime.contracts import (
    BudgetAllocation,
    BudgetOperation,
    BudgetOperationKind,
    BudgetOperationState,
    BudgetOwnerKind,
    JobClaim,
    JobStatus,
)


class BudgetConflictError(RuntimeError):
    pass


class BudgetLimitExceededError(RuntimeError):
    pass


class BudgetClaimStaleError(RuntimeError):
    pass


def _allocation(row: BudgetAllocationRow) -> BudgetAllocation:
    return BudgetAllocation(
        allocation_id=row.id,
        account_id=row.account_id,
        owner_kind=BudgetOwnerKind(row.owner_kind),
        owner_ref=row.owner_ref,
        token_grant=row.token_grant,
        tool_call_grant=row.tool_call_grant,
        token_reserved=row.token_reserved,
        tool_calls_reserved=row.tool_calls_reserved,
        token_consumed=row.token_consumed,
        tool_calls_consumed=row.tool_calls_consumed,
        status=row.status,
    )


def _operation(row: BudgetOperationRow) -> BudgetOperation:
    return BudgetOperation(
        operation_id=row.id,
        allocation_id=row.allocation_id,
        attempt_id=row.attempt_id,
        claim_generation=row.claim_generation,
        lease_token=row.lease_token,
        kind=BudgetOperationKind(row.kind),
        state=BudgetOperationState(row.state),
        idempotency_key=row.idempotency_key,
        reserved_tokens=row.reserved_tokens,
        reserved_tool_calls=row.reserved_tool_calls,
        consumed_tokens=row.consumed_tokens,
        consumed_tool_calls=row.consumed_tool_calls,
        provider_request_id=row.provider_request_id,
    )


class BudgetLedger:
    """Atomic PostgreSQL reservation ledger for the existing durable runtime."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_account(
        self,
        *,
        root_job_id: UUID,
        project_id: UUID,
        token_cap: int,
        tool_call_cap: int,
    ) -> UUID:
        if token_cap < 0 or tool_call_cap < 0:
            raise BudgetConflictError("budget caps cannot be negative")
        existing = await self._session.scalar(
            select(BudgetAccountRow)
            .where(BudgetAccountRow.root_job_id == root_job_id)
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.project_id != project_id
                or existing.token_cap != token_cap
                or existing.tool_call_cap != tool_call_cap
            ):
                raise BudgetConflictError("root Job budget account is already frozen")
            return existing.id
        account_id = uuid4()
        self._session.add(
            BudgetAccountRow(
                id=account_id,
                root_job_id=root_job_id,
                project_id=project_id,
                token_cap=token_cap,
                tool_call_cap=tool_call_cap,
                token_committed=0,
                tool_calls_committed=0,
                status="open",
            )
        )
        await self._session.flush()
        return account_id

    async def get_account_id(self, root_job_id: UUID) -> UUID:
        account_id = await self._session.scalar(
            select(BudgetAccountRow.id).where(BudgetAccountRow.root_job_id == root_job_id)
        )
        if account_id is None:
            raise BudgetConflictError("root Job has no budget account")
        return account_id

    async def allocate(
        self,
        *,
        account_id: UUID,
        owner_kind: BudgetOwnerKind,
        owner_ref: UUID,
        token_grant: int,
        tool_call_grant: int,
    ) -> BudgetAllocation:
        if token_grant < 0 or tool_call_grant < 0:
            raise BudgetConflictError("allocation grants cannot be negative")
        account = await self._session.scalar(
            select(BudgetAccountRow).where(BudgetAccountRow.id == account_id).with_for_update()
        )
        if account is None:
            raise BudgetConflictError("budget account not found")
        existing = await self._session.scalar(
            select(BudgetAllocationRow)
            .where(
                BudgetAllocationRow.account_id == account_id,
                BudgetAllocationRow.owner_kind == owner_kind.value,
                BudgetAllocationRow.owner_ref == owner_ref,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.token_grant != token_grant or existing.tool_call_grant != tool_call_grant:
                raise BudgetConflictError("budget owner already has a different grant")
            return _allocation(existing)
        if account.status != "open":
            raise BudgetConflictError("budget account is not open")
        if account.token_committed + token_grant > account.token_cap:
            raise BudgetLimitExceededError("root token budget is exhausted")
        if account.tool_calls_committed + tool_call_grant > account.tool_call_cap:
            raise BudgetLimitExceededError("root tool-call budget is exhausted")
        account.token_committed += token_grant
        account.tool_calls_committed += tool_call_grant
        row = BudgetAllocationRow(
            id=uuid4(),
            account_id=account_id,
            owner_kind=owner_kind.value,
            owner_ref=owner_ref,
            token_grant=token_grant,
            tool_call_grant=tool_call_grant,
            token_reserved=0,
            tool_calls_reserved=0,
            token_consumed=0,
            tool_calls_consumed=0,
            status="open",
        )
        self._session.add(row)
        await self._session.flush()
        return _allocation(row)

    async def get_allocation(
        self,
        *,
        account_id: UUID,
        owner_kind: BudgetOwnerKind,
        owner_ref: UUID,
    ) -> BudgetAllocation:
        row = await self._session.scalar(
            select(BudgetAllocationRow).where(
                BudgetAllocationRow.account_id == account_id,
                BudgetAllocationRow.owner_kind == owner_kind.value,
                BudgetAllocationRow.owner_ref == owner_ref,
            )
        )
        if row is None:
            raise BudgetConflictError("budget allocation not found")
        return _allocation(row)

    async def get_allocation_by_id(self, allocation_id: UUID) -> BudgetAllocation:
        row = await self._session.get(BudgetAllocationRow, allocation_id)
        if row is None:
            raise BudgetConflictError("budget allocation not found")
        return _allocation(row)

    async def reserve_operation(
        self,
        *,
        allocation_id: UUID,
        claim: JobClaim,
        kind: BudgetOperationKind,
        logical_step: str,
        physical_attempt_no: int,
        idempotency_key: str,
        request_hash: str,
        provider: str,
        model_or_tool: str,
        reserved_tokens: int = 0,
        reserved_tool_calls: int = 0,
        request_artifact_ref: str | None = None,
    ) -> BudgetOperation:
        if reserved_tokens < 0 or reserved_tool_calls < 0:
            raise BudgetConflictError("operation reservation cannot be negative")
        if kind is BudgetOperationKind.MODEL and reserved_tokens <= 0:
            raise BudgetConflictError("model operation requires a positive token reservation")
        if kind is BudgetOperationKind.TOOL and reserved_tool_calls != 1:
            raise BudgetConflictError("tool operation must reserve exactly one call")

        existing = await self._session.scalar(
            select(BudgetOperationRow)
            .where(BudgetOperationRow.idempotency_key == idempotency_key)
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.allocation_id != allocation_id
                or existing.attempt_id != claim.attempt_id
                or existing.claim_generation != claim.claim_generation
                or existing.kind != kind.value
                or existing.request_hash != request_hash
                or existing.reserved_tokens != reserved_tokens
                or existing.reserved_tool_calls != reserved_tool_calls
            ):
                raise BudgetConflictError("operation idempotency key has different content")
            return _operation(existing)

        allocation = await self._session.scalar(
            select(BudgetAllocationRow)
            .where(BudgetAllocationRow.id == allocation_id)
            .with_for_update()
        )
        if allocation is None or allocation.status != "open":
            raise BudgetConflictError("budget allocation is not open")
        if allocation.owner_ref != claim.job_id:
            raise BudgetClaimStaleError("claim does not own the allocation")
        job = await self._session.scalar(
            select(JobRow).where(JobRow.id == claim.job_id).with_for_update()
        )
        attempt = await self._session.get(AttemptRow, claim.attempt_id)
        if (
            job is None
            or attempt is None
            or job.status != JobStatus.RUNNING.value
            or job.current_generation != claim.claim_generation
            or job.lease_token != claim.lease_token
            or attempt.job_id != job.id
            or attempt.claim_generation != claim.claim_generation
            or attempt.status != "running"
        ):
            raise BudgetClaimStaleError("claim is stale or terminal")
        if (
            allocation.token_reserved + allocation.token_consumed + reserved_tokens
            > allocation.token_grant
        ):
            raise BudgetLimitExceededError("allocation token budget is exhausted")
        if (
            allocation.tool_calls_reserved + allocation.tool_calls_consumed + reserved_tool_calls
            > allocation.tool_call_grant
        ):
            raise BudgetLimitExceededError("allocation tool-call budget is exhausted")
        allocation.token_reserved += reserved_tokens
        allocation.tool_calls_reserved += reserved_tool_calls
        row = BudgetOperationRow(
            id=uuid4(),
            allocation_id=allocation_id,
            attempt_id=claim.attempt_id,
            claim_generation=claim.claim_generation,
            lease_token=claim.lease_token,
            kind=kind.value,
            logical_step=logical_step,
            physical_attempt_no=physical_attempt_no,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            provider=provider,
            model_or_tool=model_or_tool,
            state=BudgetOperationState.RESERVED.value,
            reserved_tokens=reserved_tokens,
            reserved_tool_calls=reserved_tool_calls,
            consumed_tokens=0,
            consumed_tool_calls=0,
            request_artifact_ref=request_artifact_ref,
        )
        self._session.add(row)
        await self._session.flush()
        return _operation(row)

    async def mark_dispatched(self, operation_id: UUID) -> BudgetOperation:
        row = await self._locked_operation(operation_id)
        if row.state == BudgetOperationState.DISPATCHED.value:
            return _operation(row)
        if row.state != BudgetOperationState.RESERVED.value:
            raise BudgetConflictError("only a reserved operation can be dispatched")
        row.state = BudgetOperationState.DISPATCHED.value
        row.dispatched_at = datetime.now(UTC)
        await self._session.flush()
        return _operation(row)

    async def settle(
        self,
        operation_id: UUID,
        *,
        consumed_tokens: int,
        consumed_tool_calls: int,
        provider_request_id: str | None = None,
        response_artifact_ref: str | None = None,
    ) -> BudgetOperation:
        row = await self._locked_operation(operation_id)
        if row.state == BudgetOperationState.SETTLED.value:
            if (
                row.consumed_tokens != consumed_tokens
                or row.consumed_tool_calls != consumed_tool_calls
            ):
                raise BudgetConflictError("operation is already settled with different usage")
            return _operation(row)
        if row.state != BudgetOperationState.DISPATCHED.value:
            raise BudgetConflictError("only a dispatched operation can settle")
        if not 0 <= consumed_tokens <= row.reserved_tokens:
            raise BudgetConflictError("settled tokens exceed the reservation")
        if not 0 <= consumed_tool_calls <= row.reserved_tool_calls:
            raise BudgetConflictError("settled tool calls exceed the reservation")
        allocation = await self._locked_allocation(row.allocation_id)
        allocation.token_reserved -= row.reserved_tokens
        allocation.tool_calls_reserved -= row.reserved_tool_calls
        allocation.token_consumed += consumed_tokens
        allocation.tool_calls_consumed += consumed_tool_calls
        row.consumed_tokens = consumed_tokens
        row.consumed_tool_calls = consumed_tool_calls
        row.provider_request_id = provider_request_id
        row.response_artifact_ref = response_artifact_ref
        row.state = BudgetOperationState.SETTLED.value
        row.settled_at = datetime.now(UTC)
        await self._session.flush()
        return _operation(row)

    async def mark_ambiguous(
        self,
        operation_id: UUID,
        *,
        normalized_error: str,
    ) -> BudgetOperation:
        row = await self._locked_operation(operation_id)
        if row.state == BudgetOperationState.AMBIGUOUS.value:
            return _operation(row)
        if row.state != BudgetOperationState.DISPATCHED.value:
            raise BudgetConflictError("only a dispatched operation can become ambiguous")
        allocation = await self._locked_allocation(row.allocation_id)
        allocation.token_reserved -= row.reserved_tokens
        allocation.tool_calls_reserved -= row.reserved_tool_calls
        allocation.token_consumed += row.reserved_tokens
        allocation.tool_calls_consumed += row.reserved_tool_calls
        row.consumed_tokens = row.reserved_tokens
        row.consumed_tool_calls = row.reserved_tool_calls
        row.normalized_error = normalized_error
        row.state = BudgetOperationState.AMBIGUOUS.value
        row.settled_at = datetime.now(UTC)
        await self._session.flush()
        return _operation(row)

    async def release_undispatched(self, operation_id: UUID) -> BudgetOperation:
        row = await self._locked_operation(operation_id)
        if row.state == BudgetOperationState.RELEASED.value:
            return _operation(row)
        if row.state != BudgetOperationState.RESERVED.value:
            raise BudgetConflictError("only an undispatched reservation can be released")
        allocation = await self._locked_allocation(row.allocation_id)
        allocation.token_reserved -= row.reserved_tokens
        allocation.tool_calls_reserved -= row.reserved_tool_calls
        row.state = BudgetOperationState.RELEASED.value
        row.settled_at = datetime.now(UTC)
        await self._session.flush()
        return _operation(row)

    async def close_allocation(self, allocation_id: UUID) -> BudgetAllocation:
        allocation_identity = await self._session.get(BudgetAllocationRow, allocation_id)
        if allocation_identity is None:
            raise BudgetConflictError("budget allocation not found")
        account = await self._session.scalar(
            select(BudgetAccountRow)
            .where(BudgetAccountRow.id == allocation_identity.account_id)
            .with_for_update()
        )
        allocation = await self._locked_allocation(allocation_id)
        if allocation.status == "closed":
            return _allocation(allocation)
        if allocation.token_reserved or allocation.tool_calls_reserved:
            raise BudgetConflictError("allocation still has reserved operations")
        if account is None:
            raise BudgetConflictError("budget account not found")
        account.token_committed -= allocation.token_grant - allocation.token_consumed
        account.tool_calls_committed -= allocation.tool_call_grant - allocation.tool_calls_consumed
        allocation.token_grant = allocation.token_consumed
        allocation.tool_call_grant = allocation.tool_calls_consumed
        allocation.status = "closed"
        allocation.closed_at = datetime.now(UTC)
        await self._session.flush()
        return _allocation(allocation)

    async def reconcile_reclaimed_owner(
        self,
        *,
        account_id: UUID,
        owner_kind: BudgetOwnerKind,
        owner_ref: UUID,
        normalized_error: str = "lease_reclaimed_after_dispatch",
    ) -> BudgetAllocation | None:
        """Conservatively resolve reservations and close an owner's allocation."""
        account = await self._session.scalar(
            select(BudgetAccountRow).where(BudgetAccountRow.id == account_id).with_for_update()
        )
        if account is None:
            raise BudgetConflictError("budget account not found")
        allocation = await self._session.scalar(
            select(BudgetAllocationRow)
            .where(
                BudgetAllocationRow.account_id == account_id,
                BudgetAllocationRow.owner_kind == owner_kind.value,
                BudgetAllocationRow.owner_ref == owner_ref,
            )
            .with_for_update()
        )
        if allocation is None:
            return None
        if allocation.status == "closed":
            return _allocation(allocation)
        operations = list(
            await self._session.scalars(
                select(BudgetOperationRow)
                .where(BudgetOperationRow.allocation_id == allocation.id)
                .order_by(BudgetOperationRow.id)
                .with_for_update()
            )
        )
        now = datetime.now(UTC)
        for operation in operations:
            if operation.state == BudgetOperationState.RESERVED.value:
                allocation.token_reserved -= operation.reserved_tokens
                allocation.tool_calls_reserved -= operation.reserved_tool_calls
                operation.state = BudgetOperationState.RELEASED.value
                operation.settled_at = now
            elif operation.state == BudgetOperationState.DISPATCHED.value:
                allocation.token_reserved -= operation.reserved_tokens
                allocation.tool_calls_reserved -= operation.reserved_tool_calls
                allocation.token_consumed += operation.reserved_tokens
                allocation.tool_calls_consumed += operation.reserved_tool_calls
                operation.consumed_tokens = operation.reserved_tokens
                operation.consumed_tool_calls = operation.reserved_tool_calls
                operation.normalized_error = normalized_error
                operation.state = BudgetOperationState.AMBIGUOUS.value
                operation.settled_at = now
        if allocation.token_reserved or allocation.tool_calls_reserved:
            raise BudgetConflictError("reclaimed allocation has unresolved reservations")
        account.token_committed -= allocation.token_grant - allocation.token_consumed
        account.tool_calls_committed -= allocation.tool_call_grant - allocation.tool_calls_consumed
        allocation.token_grant = allocation.token_consumed
        allocation.tool_call_grant = allocation.tool_calls_consumed
        allocation.status = "closed"
        allocation.closed_at = now
        await self._session.flush()
        return _allocation(allocation)

    async def _locked_operation(self, operation_id: UUID) -> BudgetOperationRow:
        row = await self._session.scalar(
            select(BudgetOperationRow)
            .where(BudgetOperationRow.id == operation_id)
            .with_for_update()
        )
        if row is None:
            raise BudgetConflictError("budget operation not found")
        return row

    async def _locked_allocation(self, allocation_id: UUID) -> BudgetAllocationRow:
        row = await self._session.scalar(
            select(BudgetAllocationRow)
            .where(BudgetAllocationRow.id == allocation_id)
            .with_for_update()
        )
        if row is None:
            raise BudgetConflictError("budget allocation not found")
        return row
