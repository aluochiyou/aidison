"""PostgreSQL adapter for ModelGateway's storage-neutral budget port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.providers.model_gateway import ModelBudgetContext, ModelTarget
from aidison.runtime.agent_run_budget import AgentRunBudgetOperationKind


class PostgresModelAttemptBudgetPort:
    """Open short transactions around durable physical model-attempt transitions."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def reserve_attempt(
        self,
        *,
        context: ModelBudgetContext,
        target: ModelTarget,
        ordinal: int,
    ) -> UUID:
        async with self._session_factory() as session:
            operation = await AgentRunBudgetLedger(session).reserve(
                account_id=context.account_id,
                claim=context.claim,
                kind=AgentRunBudgetOperationKind.MODEL,
                logical_step=context.logical_step,
                physical_attempt_no=ordinal,
                idempotency_key=f"{context.idempotency_prefix}:attempt:{ordinal}",
                request_hash=context.request_hash,
                provider=target.provider,
                target=f"{target.model}@{target.revision}",
                reserved_tokens=context.reserved_tokens,
                request_artifact_ref=context.request_artifact_ref,
            )
            await session.commit()
            return operation.id

    async def mark_dispatched(self, *, operation_id: object, context: ModelBudgetContext) -> None:
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).mark_dispatched(
                operation_id=self._operation_id(operation_id),
                claim=context.claim,
            )
            await session.commit()

    async def settle(
        self,
        *,
        operation_id: object,
        usage_tokens: int,
        provider_request_id: str | None,
        response_ref: str,
    ) -> None:
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).settle(
                operation_id=self._operation_id(operation_id),
                consumed_tokens=usage_tokens,
                consumed_tool_calls=0,
                provider_request_id=provider_request_id,
                response_artifact_ref=response_ref,
            )
            await session.commit()

    async def release_before_dispatch(
        self,
        *,
        operation_id: object,
        context: ModelBudgetContext,
        normalized_error: str,
    ) -> None:
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).release_undispatched(
                operation_id=self._operation_id(operation_id),
                claim=context.claim,
                normalized_error=normalized_error,
            )
            await session.commit()

    async def mark_ambiguous(self, *, operation_id: object, normalized_error: str) -> None:
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).mark_ambiguous(
                operation_id=self._operation_id(operation_id),
                normalized_error=normalized_error,
            )
            await session.commit()

    @staticmethod
    def _operation_id(value: object) -> UUID:
        if not isinstance(value, UUID):
            raise TypeError("ModelGateway budget port received a non-UUID operation id")
        return value


__all__ = ["PostgresModelAttemptBudgetPort"]
