from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from aidison.application.research import ResearchWorker
from aidison.application.service import ProjectApplication
from aidison.domain.models import DecisionRequest
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import (
    ArtifactRow,
    AttemptRow,
    BudgetOperationRow,
    DecisionRequestRow,
    JobRow,
)
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.signals import PostgresSignalBus
from aidison.infrastructure.store import PostgresDomainStore

pytestmark = [pytest.mark.live, pytest.mark.integration]


@pytest.mark.asyncio
async def test_bailian_tavily_github_research_worker_creates_evidence_bound_decision(
    tmp_path: Path,
) -> None:
    """Exercise the bounded real-provider research path against the disposable test DB."""
    if not os.getenv("DASHSCOPE_API_KEY"):
        pytest.skip("DASHSCOPE_API_KEY is not configured")
    if not os.getenv("TAVILY_API_KEY"):
        pytest.skip("TAVILY_API_KEY is not configured")
    if not os.getenv("GITHUB_API_KEY"):
        pytest.skip("GITHUB_API_KEY is not configured")
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            # tests/conftest.py rejects the runtime database before collection.
            await session.execute(text("TRUNCATE TABLE jobs, artifacts CASCADE"))
            await session.commit()
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="Live quadrotor research",
                goal=(
                    "Design a repairable DIY quadrotor with compatible propulsion and "
                    "flight-control modules."
                ),
                idempotency_key=f"live-project-{uuid4()}",
            )
            requirement, modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=project.revision,
                goal=project.goal,
                hard_constraints=(
                    "Use public technical evidence",
                    "Keep component interfaces explicit",
                ),
                preferences=("Prefer mature, repairable components",),
                available_resources=("Windows workstation", "Basic electronics tools"),
                unknowns=("Exact motor and controller compatibility",),
                modules=(
                    {
                        "key": "flight_control",
                        "name": "Flight control",
                        "responsibility": "Stabilize the aircraft and expose standard interfaces",
                        "acceptance": ("Supports documented quadrotor control",),
                        "open_questions": ("Which open autopilot stack is appropriate?",),
                    },
                    {
                        "key": "propulsion",
                        "name": "Propulsion",
                        "responsibility": "Provide compatible motor, ESC and propeller thrust",
                        "acceptance": ("Electrical and control interfaces are documented",),
                        "open_questions": ("Which sizing evidence is sufficient?",),
                    },
                ),
                idempotency_key=f"live-requirements-{uuid4()}",
            )
            basis_hash = sha256(
                f"{requirement.id}:{','.join(str(item.id) for item in modules)}".encode()
            ).hexdigest()
            root_job_id = await PostgresRuntime(session).create_job(
                project_id=project.id,
                kind="research_wave",
                basis_hash=basis_hash,
                basis_project_revision=project.revision + 1,
                profile_id="research-orchestrator",
                profile_revision=1,
                idempotency_key=f"live-research-{uuid4()}",
            )

        worker = ResearchWorker(
            session_factory=factory,
            signal_bus=PostgresSignalBus(engine),
            artifact_root=tmp_path,
            lease_seconds=60,
            poll_seconds=0.05,
        )
        worker_task = asyncio.create_task(
            worker.run_forever(worker_id=f"live-worker-{uuid4()}", concurrency=3)
        )
        try:
            async with asyncio.timeout(180):
                while True:
                    async with factory() as session:
                        root = await session.get(JobRow, root_job_id)
                        if root is not None and root.status in {"succeeded", "failed"}:
                            break
                    await asyncio.sleep(0.1)
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

        async with factory() as session:
            root = await session.get(JobRow, root_job_id)
            attempt_errors = tuple(
                await session.scalars(
                    select(AttemptRow.normalized_error)
                    .join(JobRow, AttemptRow.job_id == JobRow.id)
                    .where(JobRow.project_id == project.id)
                )
            )
            assert root is not None and root.status == "succeeded", attempt_errors

            store = PostgresDomainStore(session)
            evidence = tuple(await store.list_evidence_bindings(project.id))
            candidates = tuple(await store.list_candidates(project.id))
            child_jobs = tuple(
                await session.scalars(
                    select(JobRow).where(
                        JobRow.parent_job_id == root_job_id,
                        JobRow.kind == "delegated_research",
                    )
                )
            )
            assert len(child_jobs) == 2
            assert all(item.status == "succeeded" for item in child_jobs)
            successful_child_attempts = tuple(
                await session.scalars(
                    select(AttemptRow).where(
                        AttemptRow.job_id.in_([item.id for item in child_jobs]),
                        AttemptRow.status == "succeeded",
                    )
                )
            )
            assert len(successful_child_attempts) == 2
            child_attempt_ids = {item.id for item in successful_child_attempts}

            decision_rows = tuple(
                await session.scalars(
                    select(DecisionRequestRow).where(
                        DecisionRequestRow.project_id == project.id
                    )
                )
            )
            assert len(decision_rows) == 1
            decision = DecisionRequest.model_validate(decision_rows[0].payload)

            evidence_ids = {item.id for item in evidence}
            candidate_ids = {item.id for item in candidates}
            assert len(evidence) >= 2
            assert len(candidates) >= 2
            assert set(decision.affected_module_ids) == {item.id for item in modules}
            assert all(option.evidence_binding_ids for option in decision.options)
            assert all(option.candidate_ids for option in decision.options)
            assert all(
                set(option.evidence_binding_ids) <= evidence_ids for option in decision.options
            )
            assert all(set(option.candidate_ids) <= candidate_ids for option in decision.options)

            evidence_artifacts = tuple(
                await session.scalars(
                    select(ArtifactRow).where(
                        ArtifactRow.project_id == project.id,
                        ArtifactRow.kind.in_(("web_snapshot", "github_snapshot")),
                        ArtifactRow.status == "present",
                    )
                )
            )
            artifact_hashes = {item.content_hash for item in evidence_artifacts}
            assert {item.snapshot_hash for item in evidence} <= artifact_hashes
            assert {item.kind for item in evidence_artifacts} == {
                "web_snapshot",
                "github_snapshot",
            }
            artifact_kind_by_hash = {
                item.content_hash: item.kind for item in evidence_artifacts
            }
            assert {
                artifact_kind_by_hash[item.snapshot_hash] for item in evidence
            } == {"web_snapshot", "github_snapshot"}
            for attempt_id in child_attempt_ids:
                assert {
                    item.kind
                    for item in evidence_artifacts
                    if item.attempt_id == attempt_id
                } == {"web_snapshot", "github_snapshot"}
            for artifact in evidence_artifacts:
                content = (tmp_path / artifact.storage_key).read_bytes()
                assert len(content) == artifact.size_bytes
                assert sha256(content).hexdigest() == artifact.content_hash

            tool_operations = tuple(
                await session.scalars(
                    select(BudgetOperationRow).where(BudgetOperationRow.kind == "tool")
                )
            )
            model_operations = tuple(
                await session.scalars(
                    select(BudgetOperationRow).where(BudgetOperationRow.kind == "model")
                )
            )
            assert 2 <= len(tool_operations) <= 6
            assert {item.provider for item in tool_operations} == {"tavily", "github"}
            for attempt_id in child_attempt_ids:
                assert {
                    item.provider
                    for item in tool_operations
                    if item.attempt_id == attempt_id
                } == {"tavily", "github"}
            assert all(item.state == "settled" for item in tool_operations)
            assert len(model_operations) >= 2
            assert {item.state for item in model_operations} <= {"settled", "ambiguous"}
            assert sum(item.consumed_tokens for item in model_operations) > 0
    finally:
        await engine.dispose()
