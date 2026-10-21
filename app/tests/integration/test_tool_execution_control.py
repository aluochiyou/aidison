from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.replay import InvocationRecordingRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.infrastructure.tool_execution import PostgresReadToolExecutor
from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.runtime.agent_run_budget import AgentRunBudgetState
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim, AgentRunKind
from aidison.runtime.contracts import InvocationRecordingStatus
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.tools.dispatcher import (
    DispatchAuthority,
    ToolCallIntent,
    ToolDispatchDenied,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolEffectClass,
    ToolProfileGrant,
    ToolRiskTier,
)
from aidison.tools.execution import (
    ToolAdapterFailure,
    ToolAdapterResponse,
    ToolExecutionFailure,
    ToolExecutionReplayUnavailable,
)
from aidison.tools.source_catalog import TAVILY_SEARCH_TOOL_ID, build_source_tool_registry

pytestmark = pytest.mark.integration


def _run(project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"tool-execution-{uuid4()}",
        basis_hash=sha256(b"tool-execution-basis").hexdigest(),
        basis_project_revision=1,
        runtime_binding=RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="research",
            graph_revision="research-v1",
            state_schema_version="research-state-v1",
            profile_binding_ref="profile://research/1",
            policy_binding_ref="policy://research/1",
        ),
        thread_id=f"tool-execution-{uuid4()}",
    )


async def _scope(
    *, artifact_root: Path, allowed: bool = True
) -> tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    PostgresReadToolExecutor,
    AgentRun,
    AgentRunClaim,
    DispatchAuthority,
    ToolCallIntent,
]:
    engine = create_engine(DatabaseSettings(database_url=os.environ["TEST_DATABASE_URL"]))
    factory = create_session_factory(engine)
    async with factory() as session:
        await session.execute(text("TRUNCATE TABLE projects CASCADE"))
        await session.commit()
        project = await ProjectApplication(PostgresDomainStore(session)).create_project(
            name="Tool execution control",
            goal="Verify one physical call has one durable ledger entry",
            idempotency_key=f"tool-execution-project-{uuid4()}",
        )
        run = await AgentRunControl(session).create(_run(project.id))
        await AgentRunBudgetLedger(session).create_account(
            agent_run_id=run.id,
            token_cap=0,
            tool_call_cap=4,
        )
        await session.commit()
        claim = await AgentRunControl(session).claim_next(
            worker_id="tool-execution-worker",
            lease_seconds=60,
        )
        assert claim is not None
        await session.commit()

    allowed_ids = (TAVILY_SEARCH_TOOL_ID,) if allowed else ()
    task = TaskEnvelope(
        run_id=run.id,
        task_key="battery.search",
        basis_hash=run.basis_hash,
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=(),
        coverage_keys=("battery.current",),
        allowed_tool_ids=allowed_ids,
        budget_ref=f"budget://agent-run/{run.id}",
        idempotency_key="battery-search",
    )
    grant = ExecutionGrant(
        task_id=task.id,
        attempt_id=uuid4(),
        generation=claim.generation,
        lease_token=claim.lease_token,
        deadline_ref=f"deadline://agent-run/{run.id}",
        idempotency_prefix=task.idempotency_key,
    )
    authority = DispatchAuthority(
        task=task,
        grant=grant,
        profile=ToolProfileGrant(
            profile_ref="profile://research/1",
            allowed_tool_ids=(TAVILY_SEARCH_TOOL_ID,),
            allowed_tool_classes=("web_search",),
            allowed_effect_classes=(ToolEffectClass.READ,),
        ),
        policy=ToolDispatchPolicy(
            allowed_tool_classes=("web_search",),
            allowed_effect_classes=(ToolEffectClass.READ,),
            available_risk_tiers=(ToolRiskTier.T1,),
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    intent = ToolCallIntent(
        tool_call_id="search-1",
        tool_id=TAVILY_SEARCH_TOOL_ID,
        revision=1,
        arguments={
            "query": "6S battery discharge curve",
            "max_results": 3,
            "search_depth": "basic",
        },
        purpose_code="collect_evidence",
    )
    executor = PostgresReadToolExecutor(
        dispatcher=ToolDispatcher(build_source_tool_registry()),
        session_factory=factory,
        artifact_root=artifact_root,
    )
    return engine, factory, executor, run, claim, authority, intent


@pytest.mark.asyncio
async def test_success_replays_without_second_physical_call(tmp_path: Path) -> None:
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine, factory, executor, run, claim, authority, intent = await _scope(artifact_root=tmp_path)
    calls = 0

    async def invoke(_invocation: object) -> ToolAdapterResponse:
        nonlocal calls
        calls += 1
        return ToolAdapterResponse(
            value={"results": [{"url": "https://example.test/battery"}]},
            provider_request_id="provider-request-1",
        )

    try:
        first = await executor.execute(
            intent,
            authority=authority,
            run=run,
            claim=claim,
            provider="tavily",
            target="search-v1",
            invoke=invoke,
        )
        replayed = await executor.execute(
            intent.model_copy(update={"tool_call_id": "search-retry"}),
            authority=authority,
            run=run,
            claim=claim,
            provider="tavily",
            target="search-v1",
            invoke=invoke,
        )

        assert calls == 1
        assert first.replayed is False
        assert replayed.replayed is True
        assert replayed.value == first.value
        async with factory() as session:
            operations = await AgentRunBudgetLedger(session).list_operations_for_run(run.id)
            recordings = await InvocationRecordingRepository(session).list_for_run(
                project_id=run.project_id,
                agent_run_id=run.id,
            )
        assert len(operations) == 1
        assert operations[0].state is AgentRunBudgetState.SETTLED
        assert operations[0].consumed_tool_calls == 1
        assert len(recordings) == 1
        assert recordings[0].status is InvocationRecordingStatus.SUCCEEDED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_definite_provider_failure_is_settled_and_replayed(tmp_path: Path) -> None:
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine, factory, executor, run, claim, authority, intent = await _scope(artifact_root=tmp_path)
    calls = 0

    async def invoke(_invocation: object) -> ToolAdapterResponse:
        nonlocal calls
        calls += 1
        raise ToolAdapterFailure("provider_quota_exhausted", provider_request_id="req-429")

    try:
        with pytest.raises(ToolExecutionFailure) as first:
            await executor.execute(
                intent,
                authority=authority,
                run=run,
                claim=claim,
                provider="tavily",
                target="search-v1",
                invoke=invoke,
            )
        with pytest.raises(ToolExecutionFailure) as replayed:
            await executor.execute(
                intent,
                authority=authority,
                run=run,
                claim=claim,
                provider="tavily",
                target="search-v1",
                invoke=invoke,
            )
        assert first.value.reason_code == "provider_quota_exhausted"
        assert first.value.replayed is False
        assert replayed.value.replayed is True
        assert calls == 1
        async with factory() as session:
            operations = await AgentRunBudgetLedger(session).list_operations_for_run(run.id)
        assert len(operations) == 1
        assert operations[0].state is AgentRunBudgetState.SETTLED
        assert operations[0].consumed_tool_calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_post_dispatch_unknown_is_ambiguous_and_never_blindly_replayed(
    tmp_path: Path,
) -> None:
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine, factory, executor, run, claim, authority, intent = await _scope(artifact_root=tmp_path)
    calls = 0

    async def invoke(_invocation: object) -> ToolAdapterResponse:
        nonlocal calls
        calls += 1
        raise TimeoutError("provider connection ended after dispatch")

    try:
        with pytest.raises(TimeoutError):
            await executor.execute(
                intent,
                authority=authority,
                run=run,
                claim=claim,
                provider="tavily",
                target="search-v1",
                invoke=invoke,
            )
        with pytest.raises(ToolExecutionReplayUnavailable, match="ambiguous"):
            await executor.execute(
                intent,
                authority=authority,
                run=run,
                claim=claim,
                provider="tavily",
                target="search-v1",
                invoke=invoke,
            )
        assert calls == 1
        async with factory() as session:
            operations = await AgentRunBudgetLedger(session).list_operations_for_run(run.id)
        assert len(operations) == 1
        assert operations[0].state is AgentRunBudgetState.AMBIGUOUS
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_authorization_denial_creates_no_physical_call_or_budget_operation(
    tmp_path: Path,
) -> None:
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine, factory, executor, run, claim, authority, intent = await _scope(
        artifact_root=tmp_path,
        allowed=False,
    )
    calls = 0

    async def invoke(_invocation: object) -> ToolAdapterResponse:
        nonlocal calls
        calls += 1
        return ToolAdapterResponse(value={})

    try:
        with pytest.raises(ToolDispatchDenied, match="TaskEnvelope"):
            await executor.execute(
                intent,
                authority=authority,
                run=run,
                claim=claim,
                provider="tavily",
                target="search-v1",
                invoke=invoke,
            )
        assert calls == 0
        async with factory() as session:
            operations = await AgentRunBudgetLedger(session).list_operations_for_run(run.id)
            recordings = await InvocationRecordingRepository(session).list_for_run(
                project_id=run.project_id,
                agent_run_id=run.id,
            )
        assert operations == ()
        assert recordings == ()
    finally:
        await engine.dispose()
