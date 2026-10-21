from __future__ import annotations

import json
import os
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.replay import InvocationRecordingRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_run_budget import AgentRunBudgetOperationKind
from aidison.runtime.agent_run_events import AgentRunEventType
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.contracts import (
    BudgetOperationKind,
    FailureClass,
    InvocationRecording,
    InvocationRecordingStatus,
)
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _binding() -> RuntimeBinding:
    return RuntimeBinding(
        runtime_family=RuntimeFamily.LANGGRAPH_V1,
        runtime_revision="runtime-v1",
        graph_key="research",
        graph_revision="research-r7",
        state_schema_version="research-state-v2",
        profile_binding_ref="profile://research/default",
        policy_binding_ref="policy://research/default",
    )


@pytest.mark.asyncio
async def test_agent_run_trajectory_api_reports_recovery_without_exposing_payloads() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_budget_accounts CASCADE"))
            await session.execute(text("TRUNCATE TABLE invocation_recordings CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun trajectory fixture",
                goal="Inspect a failed provider attempt followed by recovery",
                idempotency_key=f"trajectory-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            run = await control.create(
                AgentRun(
                    project_id=project.id,
                    kind=AgentRunKind.RESEARCH,
                    idempotency_key=f"trajectory-run-{uuid4()}",
                    basis_hash=sha256(b"trajectory-basis").hexdigest(),
                    basis_project_revision=project.revision,
                    runtime_binding=_binding(),
                    thread_id=f"trajectory-thread-{uuid4()}",
                )
            )
            await control.record_queued_event(
                run_id=run.id,
                event_type=AgentRunEventType.RESEARCH_QUEUED,
                context={},
                artifact_refs=(),
            )
            claim = await control.claim_next(
                worker_id="trajectory-worker",
                lease_seconds=60,
                run_id=run.id,
            )
            assert claim is not None

            budget = AgentRunBudgetLedger(session)
            account = await budget.create_account(
                agent_run_id=run.id,
                token_cap=200,
                tool_call_cap=2,
            )
            failed = await budget.reserve(
                account_id=account.id,
                claim=claim,
                kind=AgentRunBudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=1,
                idempotency_key=f"{run.id}:research.model:1",
                request_hash=sha256(b"first-request").hexdigest(),
                provider="openai",
                target="gpt-test",
                reserved_tokens=50,
            )
            await budget.release_undispatched(
                operation_id=failed.id,
                claim=claim,
                normalized_error="quota_unavailable",
            )
            recovered = await budget.reserve(
                account_id=account.id,
                claim=claim,
                kind=AgentRunBudgetOperationKind.MODEL,
                logical_step="research.model",
                physical_attempt_no=2,
                idempotency_key=f"{run.id}:research.model:2",
                request_hash=sha256(b"second-request").hexdigest(),
                provider="openai",
                target="gpt-test",
                reserved_tokens=50,
            )
            await budget.mark_dispatched(operation_id=recovered.id, claim=claim)
            await budget.settle(
                operation_id=recovered.id,
                consumed_tokens=31,
                consumed_tool_calls=0,
                provider_request_id="provider-secret-request-id",
                response_artifact_ref="artifact://private-response",
            )
            await session.commit()

            recording = InvocationRecording(
                project_id=project.id,
                agent_run_id=run.id,
                producer_attempt_id=uuid4(),
                basis_hash=run.basis_hash,
                idempotency_key=f"{run.id}:tool:1",
                request_hash=sha256(b"private-tool-request").hexdigest(),
                kind=BudgetOperationKind.TOOL,
                provider="web-search",
                operation_name="search",
                status=InvocationRecordingStatus.PENDING,
            )
            replay = InvocationRecordingRepository(session)
            await replay.prepare(recording)
            await replay.record(
                recording.model_copy(
                    update={
                        "status": InvocationRecordingStatus.AMBIGUOUS,
                        "failure_class": FailureClass.UNKNOWN_EFFECT,
                    }
                )
            )
            await control.complete(claim=claim, status=AgentRunStatus.SUCCEEDED)
            await session.commit()

        api = create_app(factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/projects/{project.id}/agent-runs/{run.id}/trajectory"
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["schema_version"] == "agent-run-trajectory.v1"
            assert payload["summary"]["provider_attempt_count"] == 2
            assert payload["summary"]["consumed_tokens"] == 31
            assert payload["failure_analysis"]["self_recovered"] is True
            assert payload["failure_analysis"]["root_cause_inferred"] is False
            assert (
                payload["failure_analysis"]["unresolved_unknown_effect_record_count"] == 1
            )
            assert payload["provider_attempts"][0]["error_code"] == "quota_unavailable"
            assert [event["event_type"] for event in payload["lifecycle_events"]] == [
                "agent_run.queued",
                "agent_run.running",
                "agent_run.succeeded",
            ]

            rendered = json.dumps(payload)
            for private_value in (
                "provider-secret-request-id",
                "artifact://private-response",
                sha256(b"private-tool-request").hexdigest(),
                f"{run.id}:tool:1",
                "profile://research/default",
                "policy://research/default",
            ):
                assert private_value not in rendered

            not_found = await client.get(
                f"/api/projects/{uuid4()}/agent-runs/{run.id}/trajectory"
            )
            assert not_found.status_code == 404
    finally:
        await engine.dispose()
