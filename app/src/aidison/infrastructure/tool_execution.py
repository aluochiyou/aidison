"""PostgreSQL + Artifact implementation of the read-only tool execution kernel."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import session_scope
from aidison.infrastructure.replay import InvocationRecordingRepository
from aidison.research.langgraph_contracts import ExecutionGrant
from aidison.runtime.agent_run_budget import (
    AgentRunBudgetOperation,
    AgentRunBudgetOperationKind,
    AgentRunBudgetState,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim
from aidison.runtime.contracts import (
    BudgetOperationKind,
    FailureClass,
    InvocationRecording,
    InvocationRecordingStatus,
)
from aidison.runtime.replay import ReplayConflictError
from aidison.tools.dispatcher import (
    DispatchAuthority,
    ToolCallIntent,
    ToolDispatcher,
    ToolEffectClass,
)
from aidison.tools.execution import (
    ToolAdapterCallable,
    ToolAdapterFailure,
    ToolExecutionFailure,
    ToolExecutionReplayUnavailable,
    ToolExecutionResult,
)

_OUTCOME_KIND = "tool_invocation_outcome"
_REQUEST_KIND = "tool_invocation_request"


class PostgresReadToolExecutor:
    """Authorize, reserve, dispatch, record and replay one physical read.

    External writes are intentionally refused here because they require the
    approval-bound Effect Ledger.  A definite provider response, including a
    classified 4xx/5xx, consumes one tool call and is replayable.  An exception
    after dispatch is fenced as ambiguous.
    """

    def __init__(
        self,
        *,
        dispatcher: ToolDispatcher,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
    ) -> None:
        self._dispatcher = dispatcher
        self._session_factory = session_factory
        self._artifact_root = artifact_root

    async def execute(
        self,
        intent: ToolCallIntent,
        *,
        authority: DispatchAuthority,
        run: AgentRun,
        claim: AgentRunClaim,
        provider: str,
        target: str,
        invoke: ToolAdapterCallable,
    ) -> ToolExecutionResult:
        invocation = self._dispatcher.authorize(intent, authority=authority)
        self._validate_scope(
            invocation_effect=invocation.effect_class,
            run=run,
            claim=claim,
            grant=authority.grant,
            task_run_id=authority.task.run_id,
        )
        request_value = {
            "schema_version": "tool-invocation-request-v1",
            "logical_invocation_id": invocation.logical_invocation_id,
            "tool_id": invocation.tool_id,
            "revision": invocation.revision,
            "definition_hash": invocation.definition_hash,
            "purpose_code": invocation.purpose_code,
            "arguments": invocation.canonical_arguments,
            "provider": provider,
            "target": target,
        }
        request_hash = _canonical_hash(request_value)
        request_ref = await self._put_artifact(
            run=run,
            kind=_REQUEST_KIND,
            value=request_value,
        )
        requested = InvocationRecording(
            project_id=run.project_id,
            agent_run_id=run.id,
            producer_attempt_id=authority.grant.attempt_id,
            basis_hash=run.basis_hash,
            idempotency_key=invocation.logical_idempotency_key,
            request_hash=request_hash,
            kind=BudgetOperationKind.TOOL,
            provider=provider,
            operation_name=f"{invocation.tool_id}@{invocation.revision}",
            status=InvocationRecordingStatus.PENDING,
        )
        stored, prepared_here = await self._prepare_recording(requested)
        if not prepared_here:
            return await self._replay_or_recover(
                run=run,
                invocation_id=invocation.logical_invocation_id,
                requested=requested,
                stored=stored,
            )

        attempt_key = f"{invocation.logical_idempotency_key}:attempt:1"
        operation = await self._reserve(
            run=run,
            claim=claim,
            request_hash=request_hash,
            request_ref=request_ref,
            idempotency_key=attempt_key,
            logical_step=f"tool.{invocation.tool_id}",
            provider=provider,
            target=target,
        )
        if operation.state is AgentRunBudgetState.SETTLED:
            return await self._recover_settled(
                run=run,
                invocation_id=invocation.logical_invocation_id,
                requested=requested,
                operation=operation,
            )
        if operation.state is not AgentRunBudgetState.RESERVED:
            raise ToolExecutionReplayUnavailable(
                f"tool attempt is unsafe from {operation.state.value}"
            )
        await self._mark_dispatched(operation=operation, claim=claim)

        try:
            response = await invoke(invocation)
        except ToolAdapterFailure as error:
            response_ref = await self._put_artifact(
                run=run,
                kind=_OUTCOME_KIND,
                value={
                    "schema_version": "tool-invocation-outcome-v1",
                    "status": "failed",
                    "reason_code": error.reason_code,
                },
            )
            await self._settle_and_complete(
                requested=requested,
                operation=operation,
                response_ref=response_ref,
                provider_request_id=error.provider_request_id,
            )
            raise ToolExecutionFailure(error.reason_code, replayed=False) from error
        except BaseException as error:
            await self._mark_ambiguous(
                requested=requested,
                operation=operation,
                error=error,
            )
            raise

        response_ref = await self._put_artifact(
            run=run,
            kind=_OUTCOME_KIND,
            value={
                "schema_version": "tool-invocation-outcome-v1",
                "status": "succeeded",
                "value": response.value,
            },
        )
        await self._settle_and_complete(
            requested=requested,
            operation=operation,
            response_ref=response_ref,
            provider_request_id=response.provider_request_id,
        )
        return ToolExecutionResult(
            value=response.value,
            replayed=False,
            logical_invocation_id=invocation.logical_invocation_id,
            response_artifact_ref=response_ref,
        )

    @staticmethod
    def _validate_scope(
        *,
        invocation_effect: ToolEffectClass,
        run: AgentRun,
        claim: AgentRunClaim,
        grant: ExecutionGrant,
        task_run_id: object,
    ) -> None:
        if invocation_effect is not ToolEffectClass.READ:
            raise ValueError("PostgresReadToolExecutor accepts read-only tools")
        if task_run_id != run.id or claim.run_id != run.id:
            raise ValueError("tool execution scope does not belong to AgentRun")
        if grant.generation != claim.generation or grant.lease_token != claim.lease_token:
            raise ValueError("ExecutionGrant does not match AgentRun claim")

    async def _prepare_recording(
        self,
        requested: InvocationRecording,
    ) -> tuple[InvocationRecording, bool]:
        async with self._session_factory() as session:
            repository = InvocationRecordingRepository(session)
            prepared = await repository.prepare(requested)
            stored = await repository.get(requested.idempotency_key)
        if stored is None:
            raise ReplayConflictError("tool invocation preparation did not persist")
        return stored, prepared

    async def _reserve(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        request_hash: str,
        request_ref: str,
        idempotency_key: str,
        logical_step: str,
        provider: str,
        target: str,
    ) -> AgentRunBudgetOperation:
        async with self._session_factory() as session:
            ledger = AgentRunBudgetLedger(session)
            existing = await ledger.get_operation_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.request_hash != request_hash
                    or existing.provider != provider
                    or existing.target != target
                ):
                    raise ReplayConflictError("tool attempt key has different content")
                return existing
            account = await ledger.get_account_for_run(run.id)
            operation = await ledger.reserve(
                account_id=account.id,
                claim=claim,
                kind=AgentRunBudgetOperationKind.TOOL,
                logical_step=logical_step,
                physical_attempt_no=1,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                provider=provider,
                target=target,
                reserved_tool_calls=1,
                request_artifact_ref=request_ref,
            )
            await session.commit()
            return operation

    async def _mark_dispatched(
        self,
        *,
        operation: AgentRunBudgetOperation,
        claim: AgentRunClaim,
    ) -> None:
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).mark_dispatched(
                operation_id=operation.id,
                claim=claim,
            )
            await session.commit()

    async def _settle_and_complete(
        self,
        *,
        requested: InvocationRecording,
        operation: AgentRunBudgetOperation,
        response_ref: str,
        provider_request_id: str | None,
    ) -> None:
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).settle(
                operation_id=operation.id,
                consumed_tokens=0,
                consumed_tool_calls=1,
                provider_request_id=provider_request_id,
                response_artifact_ref=response_ref,
            )
            await session.commit()
        completed = requested.model_copy(
            update={
                "status": InvocationRecordingStatus.SUCCEEDED,
                "response_artifact_ref": response_ref,
            }
        )
        async with self._session_factory() as session:
            await InvocationRecordingRepository(session).record(completed)

    async def _mark_ambiguous(
        self,
        *,
        requested: InvocationRecording,
        operation: AgentRunBudgetOperation,
        error: BaseException,
    ) -> None:
        normalized = type(error).__name__.lower().replace("_", "-")[:100]
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).mark_ambiguous(
                operation_id=operation.id,
                normalized_error=normalized,
            )
            await session.commit()
        ambiguous = requested.model_copy(
            update={
                "status": InvocationRecordingStatus.AMBIGUOUS,
                "failure_class": FailureClass.UNKNOWN_EFFECT,
            }
        )
        async with self._session_factory() as session:
            await InvocationRecordingRepository(session).record(ambiguous)

    async def _replay_or_recover(
        self,
        *,
        run: AgentRun,
        invocation_id: str,
        requested: InvocationRecording,
        stored: InvocationRecording,
    ) -> ToolExecutionResult:
        if stored.status is InvocationRecordingStatus.SUCCEEDED:
            assert stored.response_artifact_ref is not None
            return await self._read_outcome(
                run=run,
                invocation_id=invocation_id,
                response_ref=stored.response_artifact_ref,
                replayed=True,
            )
        if stored.status is InvocationRecordingStatus.AMBIGUOUS:
            raise ToolExecutionReplayUnavailable("tool invocation has an ambiguous prior attempt")
        attempt_key = f"{requested.idempotency_key}:attempt:1"
        async with self._session_factory() as session:
            operation = await AgentRunBudgetLedger(session).get_operation_by_idempotency_key(
                attempt_key
            )
        if operation is not None and operation.state is AgentRunBudgetState.SETTLED:
            return await self._recover_settled(
                run=run,
                invocation_id=invocation_id,
                requested=requested,
                operation=operation,
            )
        state = "missing" if operation is None else operation.state.value
        raise ToolExecutionReplayUnavailable(f"pending tool invocation is unsafe from {state}")

    async def _recover_settled(
        self,
        *,
        run: AgentRun,
        invocation_id: str,
        requested: InvocationRecording,
        operation: AgentRunBudgetOperation,
    ) -> ToolExecutionResult:
        if operation.response_artifact_ref is None:
            raise ToolExecutionReplayUnavailable("settled tool attempt has no response artifact")
        completed = requested.model_copy(
            update={
                "status": InvocationRecordingStatus.SUCCEEDED,
                "response_artifact_ref": operation.response_artifact_ref,
            }
        )
        async with self._session_factory() as session:
            await InvocationRecordingRepository(session).record(completed)
        return await self._read_outcome(
            run=run,
            invocation_id=invocation_id,
            response_ref=operation.response_artifact_ref,
            replayed=True,
        )

    async def _put_artifact(self, *, run: AgentRun, kind: str, value: object) -> str:
        async with session_scope(self._session_factory) as session:
            artifact = await ContentAddressedArtifactStore(
                session,
                self._artifact_root,
            ).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind=kind,
                value=value,
            )
        return artifact.ref

    async def _read_outcome(
        self,
        *,
        run: AgentRun,
        invocation_id: str,
        response_ref: str,
        replayed: bool,
    ) -> ToolExecutionResult:
        async with self._session_factory() as session:
            value = await ContentAddressedArtifactStore(
                session,
                self._artifact_root,
            ).read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=response_ref,
                expected_kind=_OUTCOME_KIND,
            )
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != "tool-invocation-outcome-v1"
        ):
            raise ToolExecutionReplayUnavailable("tool outcome artifact is invalid")
        if value.get("status") == "failed":
            reason_code = value.get("reason_code")
            if not isinstance(reason_code, str):
                raise ToolExecutionReplayUnavailable("tool failure artifact is invalid")
            raise ToolExecutionFailure(reason_code, replayed=replayed)
        if value.get("status") != "succeeded" or "value" not in value:
            raise ToolExecutionReplayUnavailable("tool success artifact is invalid")
        return ToolExecutionResult(
            value=value["value"],
            replayed=replayed,
            logical_invocation_id=invocation_id,
            response_artifact_ref=response_ref,
        )


def _canonical_hash(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


__all__ = ["PostgresReadToolExecutor"]
