"""Durable physical-call control for trusted research source collection.

Collectors may use HTTP, a local allowlisted directory or an operator-provided
repository token.  They are not model-callable tools, but they still cross an
external boundary.  This adapter gives them the same reserve → dispatch →
artifact → settle discipline as model invocations.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import session_scope
from aidison.infrastructure.tool_execution import PostgresReadToolExecutor
from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.research.source_collection import (
    CollectedResearchSource,
    ResearchSourceCollectionError,
    ResearchSourceCollector,
    ResearchSourceToolRuntime,
    source_tool_ids,
)
from aidison.runtime.agent_run_budget import (
    AgentRunBudgetOperation,
    AgentRunBudgetOperationKind,
    AgentRunBudgetState,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim
from aidison.tools.dispatcher import (
    DispatchAuthority,
    ToolCallIntent,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolEffectClass,
    ToolProfileGrant,
)
from aidison.tools.execution import ToolAdapterCallable, ToolExecutionResult
from aidison.tools.source_catalog import SOURCE_TOOL_DEFINITIONS, build_source_tool_registry


@runtime_checkable
class RunScopedResearchSourceCollector(Protocol):
    """A collector that needs claim/grant ownership around its physical call."""

    async def collect_for_task(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]: ...


class SourceCollectionReplayUnavailable(RuntimeError):
    """A previous source operation cannot be safely replayed or re-dispatched."""


class _TaskBoundSourceToolRuntime(ResearchSourceToolRuntime):
    """Translate one trusted adapter call into the common durable tool kernel."""

    def __init__(
        self,
        *,
        executor: PostgresReadToolExecutor,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        configured_tool_ids: tuple[str, ...],
    ) -> None:
        specs = tuple(
            spec
            for spec, _ in SOURCE_TOOL_DEFINITIONS
            if spec.tool_id in configured_tool_ids
        )
        if {spec.tool_id for spec in specs} != set(configured_tool_ids):
            raise ValueError("configured source collector has an unregistered tool identity")
        self._executor = executor
        self._run = run
        self._claim = claim
        self._authority = DispatchAuthority(
            task=task,
            grant=grant,
            profile=ToolProfileGrant(
                profile_ref=run.runtime_binding.profile_binding_ref,
                allowed_tool_ids=tuple(sorted(configured_tool_ids)),
                allowed_tool_classes=tuple(sorted({spec.tool_class for spec in specs})),
                allowed_effect_classes=(ToolEffectClass.READ,),
            ),
            policy=ToolDispatchPolicy(
                allowed_tool_classes=tuple(sorted({spec.tool_class for spec in specs})),
                allowed_effect_classes=(ToolEffectClass.READ,),
                available_risk_tiers=tuple(
                    sorted({spec.risk_tier for spec in specs}, key=str)
                ),
            ),
            deadline=claim.lease_expires_at,
        )

    async def execute(
        self,
        *,
        tool_id: str,
        arguments: dict[str, object],
        purpose_code: str,
        provider: str,
        target: str,
        invoke: ToolAdapterCallable,
    ) -> ToolExecutionResult:
        canonical = json.dumps(
            {
                "tool_id": tool_id,
                "purpose_code": purpose_code,
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return await self._executor.execute(
            ToolCallIntent(
                tool_call_id=f"source-{sha256(canonical.encode()).hexdigest()[:32]}",
                tool_id=tool_id,
                revision=1,
                arguments=arguments,
                purpose_code=purpose_code,
            ),
            authority=self._authority,
            run=self._run,
            claim=self._claim,
            provider=provider,
            target=target,
            invoke=invoke,
        )


class BudgetedResearchSourceCollector:
    """Bind configured source adapters to durable per-physical-call control.

    Tool-aware production collectors use the common Tool Dispatcher and one
    budget/recording entry per HTTP or file read.  Legacy/custom collectors
    without declared tool identities retain the old whole-collection replay
    path so tests and explicit extensions fail closed rather than silently
    receiving broad source authority.
    """

    def __init__(
        self,
        *,
        collector: ResearchSourceCollector,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
    ) -> None:
        self._collector = collector
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._tool_ids = source_tool_ids(collector)
        self._tool_executor = PostgresReadToolExecutor(
            dispatcher=ToolDispatcher(build_source_tool_registry()),
            session_factory=session_factory,
            artifact_root=artifact_root,
        )

    @property
    def tool_ids(self) -> tuple[str, ...]:
        return self._tool_ids

    async def collect_for_task(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]:
        if self._tool_ids:
            runtime = _TaskBoundSourceToolRuntime(
                executor=self._tool_executor,
                run=run,
                claim=claim,
                task=task,
                grant=grant,
                configured_tool_ids=self._tool_ids,
            )
            return await self._collector.collect(
                run=run,
                task=task,
                question=question,
                tool_runtime=runtime,
            )
        request_ref, request_hash = await self._persist_request(
            run=run,
            task=task,
            question=question,
        )
        operation = await self._reserve(
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            request_ref=request_ref,
            request_hash=request_hash,
        )
        if operation.state is AgentRunBudgetState.SETTLED:
            if operation.response_artifact_ref is None:
                raise SourceCollectionReplayUnavailable(
                    "settled source collection has no replay artifact"
                )
            return await self._read_outcome(
                run=run,
                response_ref=operation.response_artifact_ref,
            )
        if operation.state is not AgentRunBudgetState.RESERVED:
            raise SourceCollectionReplayUnavailable(
                f"source collection retry is unsafe from {operation.state.value}"
            )
        await self._mark_dispatched(operation_id=operation.id, claim=claim)
        try:
            sources = await self._collector.collect(run=run, task=task, question=question)
        except ResearchSourceCollectionError as error:
            response_ref = await self._persist_failure_outcome(
                run=run,
                task=task,
                reason_code=error.reason_code,
            )
            await self._settle(
                operation_id=operation.id,
                response_ref=response_ref,
                provider_request_id=None,
            )
            raise
        except BaseException as error:
            await self._mark_ambiguous(operation_id=operation.id, error=error)
            raise

        response_ref = await self._persist_success_outcome(
            run=run,
            task=task,
            sources=sources,
        )
        await self._settle(
            operation_id=operation.id,
            response_ref=response_ref,
            provider_request_id=None,
        )
        return sources

    async def _persist_request(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
    ) -> tuple[str, str]:
        value = {
            "task_id": str(task.id),
            "task_key": task.task_key,
            "question": question,
            "collector": type(self._collector).__name__,
        }
        async with session_scope(self._session_factory) as session:
            artifact = await ContentAddressedArtifactStore(
                session, self._artifact_root
            ).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="source_collection_request",
                value=value,
            )
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return artifact.ref, sha256(canonical.encode()).hexdigest()

    async def _reserve(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        request_ref: str,
        request_hash: str,
    ) -> AgentRunBudgetOperation:
        async with self._session_factory() as session:
            ledger = AgentRunBudgetLedger(session)
            account = await ledger.get_account_for_run(run.id)
            operation = await ledger.reserve(
                account_id=account.id,
                claim=claim,
                kind=AgentRunBudgetOperationKind.TOOL,
                logical_step=f"source_collection.{task.task_key}",
                physical_attempt_no=1,
                idempotency_key=f"{grant.idempotency_prefix}:source-collection:1",
                request_hash=request_hash,
                provider=type(self._collector).__name__.lower(),
                target="trusted-source-collection.v1",
                reserved_tool_calls=1,
                request_artifact_ref=request_ref,
            )
            await session.commit()
            return operation

    async def _mark_dispatched(self, *, operation_id: UUID, claim: AgentRunClaim) -> None:
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).mark_dispatched(
                operation_id=operation_id,
                claim=claim,
            )
            await session.commit()

    async def _settle(
        self,
        *,
        operation_id: UUID,
        response_ref: str,
        provider_request_id: str | None,
    ) -> None:
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).settle(
                operation_id=operation_id,
                consumed_tokens=0,
                consumed_tool_calls=1,
                provider_request_id=provider_request_id,
                response_artifact_ref=response_ref,
            )
            await session.commit()

    async def _mark_ambiguous(self, *, operation_id: UUID, error: BaseException) -> None:
        error_code = f"{type(error).__name__}".lower().replace("_", "-")[:100]
        async with self._session_factory() as session:
            await AgentRunBudgetLedger(session).mark_ambiguous(
                operation_id=operation_id,
                normalized_error=error_code,
            )
            await session.commit()

    async def _persist_success_outcome(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        sources: tuple[CollectedResearchSource, ...],
    ) -> str:
        value = {
            "task_id": str(task.id),
            "status": "succeeded",
            "sources": [source.model_dump(mode="json") for source in sources],
        }
        async with session_scope(self._session_factory) as session:
            artifact = await ContentAddressedArtifactStore(
                session, self._artifact_root
            ).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="source_collection_outcome",
                value=value,
            )
        return artifact.ref

    async def _persist_failure_outcome(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        reason_code: str,
    ) -> str:
        value = {
            "task_id": str(task.id),
            "status": "failed",
            "reason_code": reason_code,
            "sources": [],
        }
        async with session_scope(self._session_factory) as session:
            artifact = await ContentAddressedArtifactStore(
                session, self._artifact_root
            ).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="source_collection_outcome",
                value=value,
            )
        return artifact.ref

    async def _read_outcome(
        self,
        *,
        run: AgentRun,
        response_ref: str,
    ) -> tuple[CollectedResearchSource, ...]:
        async with self._session_factory() as session:
            value = await ContentAddressedArtifactStore(session, self._artifact_root).read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=response_ref,
                expected_kind="source_collection_outcome",
            )
        if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
            raise SourceCollectionReplayUnavailable("source collection replay payload is invalid")
        status = value.get("status")
        if status == "succeeded":
            return tuple(CollectedResearchSource.model_validate(item) for item in value["sources"])
        if status == "failed":
            reason_code = value.get("reason_code")
            if not isinstance(reason_code, str):
                raise SourceCollectionReplayUnavailable(
                    "source collection failure replay payload is invalid"
                )
            raise ResearchSourceCollectionError(reason_code)
        raise SourceCollectionReplayUnavailable("source collection replay status is invalid")


__all__ = [
    "BudgetedResearchSourceCollector",
    "RunScopedResearchSourceCollector",
    "SourceCollectionReplayUnavailable",
]
