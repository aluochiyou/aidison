from __future__ import annotations

import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.research.source_collection import (
    CollectedResearchSource,
    ResearchSourceCollectionError,
)
from aidison.research.source_collection_control import (
    BudgetedResearchSourceCollector,
    SourceCollectionReplayUnavailable,
)
from aidison.research.source_observations import SourceIdentity, SourceKind
from aidison.runtime.agent_run_budget import AgentRunBudgetState
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim, AgentRunKind
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


class _RecordingCollector:
    def __init__(self, *, reason_code: str | None = None) -> None:
        self.reason_code = reason_code
        self.call_count = 0

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]:
        self.call_count += 1
        assert run.id == task.run_id
        assert question
        if self.reason_code is not None:
            raise ResearchSourceCollectionError(self.reason_code)
        return (
            CollectedResearchSource(
                key="replay-source",
                source=SourceIdentity(
                    kind=SourceKind.WEB,
                    provider="fixture-provider",
                    canonical_locator="https://example.test/replay-source",
                ),
                normalized_document="A deterministic source collection snapshot.",
                media_type="text/plain",
                representation="normalized-document-v1",
                parser_revision="fixture-parser-v1",
                observed_at=datetime.now(UTC),
                coverage_source_kinds=("evidence",),
            ),
        )


def _run(*, project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"source-collection-control-{uuid4()}",
        basis_hash=sha256(b"source-collection-control-basis").hexdigest(),
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
        thread_id=f"source-collection-control-{uuid4()}",
    )


async def _build_controlled_collector(
    *,
    artifact_root: Path,
    collector: _RecordingCollector,
) -> tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    BudgetedResearchSourceCollector,
    AgentRun,
    AgentRunClaim,
    TaskEnvelope,
    ExecutionGrant,
]:
    database_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    async with factory() as session:
        await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
        await session.commit()
        project = await ProjectApplication(PostgresDomainStore(session)).create_project(
            name="Source collection replay fixture",
            goal="Verify deterministic source collection outcome replay",
            idempotency_key=f"source-collection-project-{uuid4()}",
        )
        run = await AgentRunControl(session).create(_run(project_id=project.id))
        await AgentRunBudgetLedger(session).create_account(
            agent_run_id=run.id,
            token_cap=0,
            tool_call_cap=1,
        )
        await session.commit()
        claim = await AgentRunControl(session).claim_next(
            worker_id="source-collection-worker",
            lease_seconds=60,
        )
        assert claim is not None
        await session.commit()

    task = TaskEnvelope(
        run_id=run.id,
        task_key="source-replay",
        basis_hash=run.basis_hash,
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=(),
        coverage_keys=("research.answer",),
        allowed_tool_ids=(),
        budget_ref="budget://source-collection/1",
        idempotency_key="source-collection-task-1",
    )
    grant = ExecutionGrant(
        task_id=task.id,
        attempt_id=uuid4(),
        generation=claim.generation,
        lease_token=claim.lease_token,
        deadline_ref="deadline://source-collection/1",
        idempotency_prefix="source-collection-task-1",
    )
    controlled = BudgetedResearchSourceCollector(
        collector=collector,
        session_factory=factory,
        artifact_root=artifact_root,
    )
    return engine, factory, controlled, run, claim, task, grant


async def _read_outcome_artifact(
    *,
    factory: async_sessionmaker[AsyncSession],
    artifact_root: Path,
    run: AgentRun,
) -> dict[str, object]:
    async with factory() as session:
        operations = await AgentRunBudgetLedger(session).list_operations_for_run(run.id)
        assert len(operations) == 1
        operation = operations[0]
        assert operation.state is AgentRunBudgetState.SETTLED
        assert operation.response_artifact_ref is not None
        value = await ContentAddressedArtifactStore(session, artifact_root).read_json_ref(
            project_id=run.project_id,
            basis_hash=run.basis_hash,
            ref=operation.response_artifact_ref,
            expected_kind="source_collection_outcome",
        )
    assert isinstance(value, dict)
    return value


@pytest.mark.asyncio
async def test_settled_success_replays_without_calling_collector_again(tmp_path: Path) -> None:
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("TEST_DATABASE_URL is not configured")
    collector = _RecordingCollector()
    engine, factory, controlled, run, claim, task, grant = await _build_controlled_collector(
        artifact_root=tmp_path,
        collector=collector,
    )
    try:
        first = await controlled.collect_for_task(
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            question="What does the source say?",
        )
        replayed = await controlled.collect_for_task(
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            question="What does the source say?",
        )

        assert replayed == first
        assert collector.call_count == 1
        outcome = await _read_outcome_artifact(
            factory=factory,
            artifact_root=tmp_path,
            run=run,
        )
        assert outcome["status"] == "succeeded"
        assert outcome["sources"] == [first[0].model_dump(mode="json")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_settled_failure_replays_reason_without_calling_collector_again(
    tmp_path: Path,
) -> None:
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("TEST_DATABASE_URL is not configured")
    collector = _RecordingCollector(reason_code="provider_temporarily_unavailable")
    engine, factory, controlled, run, claim, task, grant = await _build_controlled_collector(
        artifact_root=tmp_path,
        collector=collector,
    )
    try:
        with pytest.raises(ResearchSourceCollectionError) as first_failure:
            await controlled.collect_for_task(
                run=run,
                claim=claim,
                task=task,
                grant=grant,
                question="What does the source say?",
            )
        with pytest.raises(ResearchSourceCollectionError) as replayed_failure:
            await controlled.collect_for_task(
                run=run,
                claim=claim,
                task=task,
                grant=grant,
                question="What does the source say?",
            )

        assert first_failure.value.reason_code == "provider_temporarily_unavailable"
        assert replayed_failure.value.reason_code == first_failure.value.reason_code
        assert collector.call_count == 1
        outcome = await _read_outcome_artifact(
            factory=factory,
            artifact_root=tmp_path,
            run=run,
        )
        assert outcome == {
            "task_id": str(task.id),
            "status": "failed",
            "reason_code": "provider_temporarily_unavailable",
            "sources": [],
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_state",
    [AgentRunBudgetState.DISPATCHED, AgentRunBudgetState.AMBIGUOUS],
)
async def test_unsettled_retry_fails_closed_without_calling_collector(
    tmp_path: Path,
    unsafe_state: AgentRunBudgetState,
) -> None:
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("TEST_DATABASE_URL is not configured")
    collector = _RecordingCollector()
    engine, _, controlled, run, claim, task, grant = await _build_controlled_collector(
        artifact_root=tmp_path,
        collector=collector,
    )
    try:
        request_ref, request_hash = await controlled._persist_request(
            run=run,
            task=task,
            question="What does the source say?",
        )
        operation = await controlled._reserve(
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            request_ref=request_ref,
            request_hash=request_hash,
        )
        await controlled._mark_dispatched(operation_id=operation.id, claim=claim)
        if unsafe_state is AgentRunBudgetState.AMBIGUOUS:
            await controlled._mark_ambiguous(
                operation_id=operation.id,
                error=RuntimeError("unknown physical call outcome"),
            )

        with pytest.raises(
            SourceCollectionReplayUnavailable,
            match=f"unsafe from {unsafe_state.value}",
        ):
            await controlled.collect_for_task(
                run=run,
                claim=claim,
                task=task,
                grant=grant,
                question="What does the source say?",
            )
        assert collector.call_count == 0
    finally:
        await engine.dispose()
