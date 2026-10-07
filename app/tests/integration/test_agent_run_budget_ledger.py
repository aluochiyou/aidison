from __future__ import annotations

import os
from datetime import timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text, update

from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_run_budget import (
    AgentRunBudgetConflictError,
    AgentRunBudgetLedger,
    AgentRunBudgetLimitExceededError,
)
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.model_budget_port import PostgresModelAttemptBudgetPort
from aidison.infrastructure.orm import AgentRunRow
from aidison.infrastructure.store import PostgresDomainStore
from aidison.providers.model_gateway import (
    FallbackPolicy,
    ModelBudgetContext,
    ModelGateway,
    ModelInvocationRequest,
    ModelTarget,
    RetryPolicy,
)
from aidison.runtime.agent_run_budget import AgentRunBudgetOperationKind, AgentRunBudgetState
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, utc_now
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _binding() -> RuntimeBinding:
    return RuntimeBinding(
        runtime_family=RuntimeFamily.LANGGRAPH_V1,
        runtime_revision="runtime-v1",
        graph_key="research",
        graph_revision="r3",
        state_schema_version="state-v1",
        profile_binding_ref="profile://research/1",
        policy_binding_ref="policy://research/1",
    )


def _run(*, project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"agent-run-budget-{uuid4()}",
        basis_hash=sha256(b"agent-run-budget-basis").hexdigest(),
        basis_project_revision=1,
        runtime_binding=_binding(),
        thread_id=f"agent-run-budget-{uuid4()}",
    )


class _GatewayAdapter:
    async def invoke(
        self, *, request: ModelInvocationRequest, target: ModelTarget
    ) -> dict[str, object]:
        return {
            "response_ref": "artifact://budgeted-provider-response",
            "provider_request_id": "budgeted-provider-request",
            "usage_tokens": 21,
        }


class _Permit:
    async def release(self) -> None:
        return None


class _Quota:
    async def acquire(self, *, bucket_key: str, deadline: object) -> _Permit:
        return _Permit()


class _Circuit:
    async def allow_request(self, *, key: str, deadline: object) -> bool:
        return True

    async def record_success(self, *, key: str) -> None:
        return None

    async def record_failure(self, *, key: str, failure: object) -> None:
        return None


@pytest.mark.asyncio
async def test_agent_run_budget_reservation_settlement_and_idempotent_replay() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_budget_accounts CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun budget fixture",
                goal="Verify a physical model call reserves and settles one Run-scoped budget",
                idempotency_key=f"agent-run-budget-project-{uuid4()}",
            )
            run_control = AgentRunControl(session)
            run = await run_control.create(_run(project_id=project.id))
            claim = await run_control.claim_next(worker_id="budget-worker-a", lease_seconds=60)
            assert claim is not None

            ledger = AgentRunBudgetLedger(session)
            account = await ledger.create_account(
                agent_run_id=run.id,
                token_cap=100,
                tool_call_cap=3,
            )
            by_run = await ledger.get_account_for_run(run.id)
            assert by_run == account
            operation = await ledger.reserve(
                account_id=account.id,
                claim=claim,
                kind=AgentRunBudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=1,
                idempotency_key=f"{run.id}:research.model:1",
                request_hash=sha256(b"request-a").hexdigest(),
                provider="provider-a",
                target="model-a",
                reserved_tokens=40,
            )
            replayed = await ledger.reserve(
                account_id=account.id,
                claim=claim,
                kind=AgentRunBudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=1,
                idempotency_key=f"{run.id}:research.model:1",
                request_hash=sha256(b"request-a").hexdigest(),
                provider="provider-a",
                target="model-a",
                reserved_tokens=40,
            )
            assert replayed.id == operation.id
            assert operation.state is AgentRunBudgetState.RESERVED

            dispatched = await ledger.mark_dispatched(operation_id=operation.id, claim=claim)
            settled = await ledger.settle(
                operation_id=operation.id,
                consumed_tokens=31,
                consumed_tool_calls=0,
                provider_request_id="provider-request-1",
                response_artifact_ref="artifact://model-response/1",
            )
            assert dispatched.state is AgentRunBudgetState.DISPATCHED
            assert settled.state is AgentRunBudgetState.SETTLED
            assert settled.consumed_tokens == 31
            assert settled.reserved_tokens == 40
            assert settled.provider_request_id == "provider-request-1"

            settled_replay = await ledger.settle(
                operation_id=operation.id,
                consumed_tokens=31,
                consumed_tool_calls=0,
                provider_request_id="provider-request-1",
                response_artifact_ref="artifact://model-response/1",
            )
            assert settled_replay == settled
            refreshed_account = await ledger.get_account(account.id)
            assert refreshed_account.token_reserved == 0
            assert refreshed_account.token_consumed == 31
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_model_gateway_uses_postgres_budget_port_for_a_real_physical_attempt() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_budget_accounts CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun gateway budget fixture",
                goal="Verify Provider Gateway persists one physical attempt budget operation",
                idempotency_key=f"agent-run-gateway-budget-project-{uuid4()}",
            )
            run_control = AgentRunControl(session)
            run = await run_control.create(_run(project_id=project.id))
            claim = await run_control.claim_next(
                worker_id="gateway-budget-worker", lease_seconds=60
            )
            assert claim is not None
            account = await AgentRunBudgetLedger(session).create_account(
                agent_run_id=run.id,
                token_cap=50,
                tool_call_cap=0,
            )
            await session.commit()

        target = ModelTarget(
            provider="provider-a",
            model="model-a",
            revision="2026-09",
            credential_pool_id="pool-a",
            quota_group="research",
            capabilities=("structured_output",),
        )
        request = ModelInvocationRequest(
            logical_invocation_id=uuid4(),
            run_id=run.id,
            task_id=uuid4(),
            basis_hash=run.basis_hash,
            prompt_ref="artifact+sha256://" + "a" * 64 + "/prompt",
            required_capabilities=("structured_output",),
            targets=(target,),
            retry_policy=RetryPolicy(max_attempts_per_target=1, base_backoff_seconds=0),
            fallback_policy=FallbackPolicy.NONE,
            deadline=utc_now() + timedelta(minutes=1),
            budget_context=ModelBudgetContext(
                account_id=account.id,
                claim=claim,
                logical_step="research.model",
                idempotency_prefix=f"{run.id}:research.model",
                request_hash=sha256(b"gateway-budget-request").hexdigest(),
                reserved_tokens=40,
            ),
        )
        result = await ModelGateway(
            adapter=_GatewayAdapter(),
            quota=_Quota(),
            circuit=_Circuit(),
            budget=PostgresModelAttemptBudgetPort(session_factory=factory),
        ).invoke(request)
        assert result.status == "succeeded"

        async with factory() as session:
            refreshed_account = await AgentRunBudgetLedger(session).get_account(account.id)
            assert refreshed_account.token_reserved == 0
            assert refreshed_account.token_consumed == 21
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_claim_cannot_reserve_or_dispatch_but_dispatched_operation_can_settle() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_budget_accounts CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun budget stale claim fixture",
                goal="Verify stale ownership cannot start a new cost but cannot erase a sent cost",
                idempotency_key=f"agent-run-budget-stale-project-{uuid4()}",
            )
            run_control = AgentRunControl(session)
            run = await run_control.create(_run(project_id=project.id))
            first_claim = await run_control.claim_next(
                worker_id="budget-worker-a", lease_seconds=60
            )
            assert first_claim is not None
            ledger = AgentRunBudgetLedger(session)
            account = await ledger.create_account(
                agent_run_id=run.id,
                token_cap=100,
                tool_call_cap=3,
            )
            sent_operation = await ledger.reserve(
                account_id=account.id,
                claim=first_claim,
                kind=AgentRunBudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=1,
                idempotency_key=f"{run.id}:research.model:sent",
                request_hash=sha256(b"request-sent").hexdigest(),
                provider="provider-a",
                target="model-a",
                reserved_tokens=40,
            )
            await ledger.mark_dispatched(operation_id=sent_operation.id, claim=first_claim)

            await session.execute(
                update(AgentRunRow)
                .where(AgentRunRow.id == run.id)
                .values(lease_expires_at=utc_now() - timedelta(seconds=1))
            )
            second_claim = await run_control.claim_next(
                worker_id="budget-worker-b", lease_seconds=60
            )
            assert second_claim is not None
            assert second_claim.generation == first_claim.generation + 1

            with pytest.raises(AgentRunBudgetConflictError, match="stale"):
                await ledger.reserve(
                    account_id=account.id,
                    claim=first_claim,
                    kind=AgentRunBudgetOperationKind.MODEL,
                    logical_step="research.model",
                    physical_attempt_no=2,
                    idempotency_key=f"{run.id}:research.model:stale",
                    request_hash=sha256(b"request-stale").hexdigest(),
                    provider="provider-a",
                    target="model-a",
                    reserved_tokens=40,
                )
            with pytest.raises(AgentRunBudgetConflictError, match="stale"):
                await ledger.mark_dispatched(operation_id=sent_operation.id, claim=first_claim)

            settled = await ledger.settle(
                operation_id=sent_operation.id,
                consumed_tokens=35,
                consumed_tool_calls=0,
                provider_request_id="provider-request-after-takeover",
                response_artifact_ref="artifact://model-response/takeover",
            )
            assert settled.state is AgentRunBudgetState.SETTLED
            assert settled.claim_generation == first_claim.generation
            assert (await ledger.get_account(account.id)).token_consumed == 35
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_undispatched_reservation_releases_but_ambiguous_sent_call_keeps_budget_held() -> (
    None
):
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_budget_accounts CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun budget ambiguity fixture",
                goal="Verify no provider-bound reservation is released after an uncertain send",
                idempotency_key=f"agent-run-budget-ambiguity-project-{uuid4()}",
            )
            run_control = AgentRunControl(session)
            run = await run_control.create(_run(project_id=project.id))
            claim = await run_control.claim_next(worker_id="budget-worker", lease_seconds=60)
            assert claim is not None
            ledger = AgentRunBudgetLedger(session)
            account = await ledger.create_account(
                agent_run_id=run.id,
                token_cap=40,
                tool_call_cap=1,
            )
            undispatched = await ledger.reserve(
                account_id=account.id,
                claim=claim,
                kind=AgentRunBudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=1,
                idempotency_key=f"{run.id}:research.model:before-send",
                request_hash=sha256(b"request-before-send").hexdigest(),
                provider="provider-a",
                target="model-a",
                reserved_tokens=40,
            )
            released = await ledger.release_undispatched(
                operation_id=undispatched.id,
                claim=claim,
                normalized_error="quota_rejected_before_dispatch",
            )
            assert released.state is AgentRunBudgetState.RELEASED
            assert (await ledger.get_account(account.id)).token_reserved == 0

            sent = await ledger.reserve(
                account_id=account.id,
                claim=claim,
                kind=AgentRunBudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=2,
                idempotency_key=f"{run.id}:research.model:unknown-usage",
                request_hash=sha256(b"request-unknown-usage").hexdigest(),
                provider="provider-a",
                target="model-a",
                reserved_tokens=40,
            )
            await ledger.mark_dispatched(operation_id=sent.id, claim=claim)
            ambiguous = await ledger.mark_ambiguous(
                operation_id=sent.id,
                normalized_error="provider_usage_unavailable_after_dispatch",
            )
            assert ambiguous.state is AgentRunBudgetState.AMBIGUOUS
            assert (await ledger.get_account(account.id)).token_reserved == 40
            with pytest.raises(AgentRunBudgetLimitExceededError, match="exhausted"):
                await ledger.reserve(
                    account_id=account.id,
                    claim=claim,
                    kind=AgentRunBudgetOperationKind.MODEL,
                    logical_step="research.model",
                    physical_attempt_no=3,
                    idempotency_key=f"{run.id}:research.model:must-not-overrun",
                    request_hash=sha256(b"request-must-not-overrun").hexdigest(),
                    provider="provider-a",
                    target="model-a",
                    reserved_tokens=1,
                )
            await session.commit()
    finally:
        await engine.dispose()
