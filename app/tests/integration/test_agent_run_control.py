from __future__ import annotations

import os
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection, sql
from sqlalchemy import text, update

from aidison.application.agent_run_replay import (
    AgentRunReplayBundleService,
    ReplayCheckpointVerifierUnavailableError,
)
from aidison.application.event_replay import AgentRunShadowProjectionService
from aidison.application.langgraph_execution import LangGraphCheckpointBridge
from aidison.application.langgraph_worker import ClaimedGraphRun, LangGraphOrchestrationWorker
from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_runs import AgentRunConflictError, AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import AgentRunRow
from aidison.infrastructure.replay import InvocationRecordingRepository
from aidison.infrastructure.replay_snapshots import EventReplaySnapshotRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_run_events import AgentRunEventType
from aidison.runtime.agent_runs import (
    AdmittedCheckpointRef,
    AgentRun,
    AgentRunKind,
    AgentRunStatus,
    utc_now,
)
from aidison.runtime.checkpointing import (
    CheckpointRuntime,
    CheckpointSettings,
    normalize_psycopg_url,
)
from aidison.runtime.contracts import (
    BudgetOperationKind,
    InvocationRecording,
    InvocationRecordingStatus,
)
from aidison.runtime.graphs import GraphRegistry, RegisteredGraph
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.runtime.minimal_graph import (
    Command,
    admitted_checkpoint_config,
    build_minimal_checkpointed_graph,
)

pytestmark = pytest.mark.integration


class _CheckpointReader:
    """Minimal read-only saver double; it never exposes a graph execution API."""

    def __init__(self) -> None:
        self.configs: list[dict[str, dict[str, str]]] = []

    async def aget_tuple(
        self,
        config: dict[str, dict[str, str]],
    ) -> SimpleNamespace:
        self.configs.append(config)
        return SimpleNamespace(config=config)


def _binding(**changes: object) -> RuntimeBinding:
    values: dict[str, object] = {
        "runtime_family": RuntimeFamily.LANGGRAPH_V1,
        "runtime_revision": "runtime-v1",
        "graph_key": "research",
        "graph_revision": "r0",
        "state_schema_version": "state-v1",
        "profile_binding_ref": "profile://research/1",
        "policy_binding_ref": "policy://research/1",
    }
    values.update(changes)
    return RuntimeBinding.model_validate(values)


def _run(*, project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"agent-run-{uuid4()}",
        basis_hash=sha256(b"agent-run-basis").hexdigest(),
        basis_project_revision=1,
        runtime_binding=_binding(),
        thread_id=f"run-{uuid4()}",
    )


@pytest.mark.asyncio
async def test_agent_run_claim_fences_stale_worker_and_persists_admitted_checkpoint() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="LangGraph AgentRun control fixture",
                goal="Verify thin durable run ownership and stale result fencing",
                idempotency_key=f"agent-run-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            created = await control.create(_run(project_id=project.id))
            await session.commit()

            first_claim = await control.claim_next(worker_id="worker-a", lease_seconds=60)
            assert first_claim is not None
            claimed = await control.get(created.id)
            assert claimed is not None
            assert claimed.started_at is not None
            first_started_at = claimed.started_at
            await session.execute(
                update(AgentRunRow)
                .where(AgentRunRow.id == created.id)
                .values(lease_expires_at=utc_now() - timedelta(seconds=1))
            )
            await session.commit()

            second_claim = await control.claim_next(worker_id="worker-b", lease_seconds=60)
            assert second_claim is not None
            assert second_claim.generation == first_claim.generation + 1
            reclaimed = await control.get(created.id)
            assert reclaimed is not None
            assert reclaimed.started_at == first_started_at
            checkpoint = AdmittedCheckpointRef(
                thread_id=created.thread_id,
                checkpoint_id="checkpoint-2",
                graph_revision="r0",
                state_schema_version="state-v1",
                generation=second_claim.generation,
            )
            admitted = await control.admit_checkpoint(claim=second_claim, checkpoint=checkpoint)
            assert admitted.admitted_checkpoint == checkpoint

            with pytest.raises(AgentRunConflictError, match="stale"):
                await control.admit_checkpoint(claim=first_claim, checkpoint=checkpoint)

            completed = await control.complete(
                claim=second_claim,
                status=AgentRunStatus.SUCCEEDED,
            )
            assert completed.status is AgentRunStatus.SUCCEEDED
            assert completed.completed_at is not None
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_run_verified_snapshot_tail_replay_matches_control_relation() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

        async with factory() as session:
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun snapshot fixture",
                goal="Verify lifecycle snapshot plus event tail without graph recovery",
                idempotency_key=f"agent-run-snapshot-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            created = await control.create(_run(project_id=project.id))
            await control.record_queued_event(
                run_id=created.id,
                event_type=AgentRunEventType.RESEARCH_QUEUED,
                context={},
                artifact_refs=(),
            )
            await session.commit()

        async with factory() as session:
            service = AgentRunShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                AgentRunControl(session),
            )
            snapshot = await service.create_verified_snapshot(
                project_id=project.id,
                agent_run_id=created.id,
            )
            duplicate = await service.create_verified_snapshot(
                project_id=project.id,
                agent_run_id=created.id,
            )
            await session.commit()

            assert snapshot.aggregate_version == 1
            assert duplicate == snapshot

        async with factory() as session:
            control = AgentRunControl(session)
            claim = await control.claim_next(worker_id="worker-a", lease_seconds=60)
            assert claim is not None
            completed = await control.complete(claim=claim, status=AgentRunStatus.SUCCEEDED)
            await session.commit()
            assert completed.status is AgentRunStatus.SUCCEEDED

        async with factory() as session:
            verification = await AgentRunShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                AgentRunControl(session),
            ).verify(
                project_id=project.id,
                agent_run_id=created.id,
            )

            assert verification.snapshot == snapshot
            assert verification.snapshot_matches_full_replay
            assert verification.snapshot_tail_hash == verification.full_replay_hash
            assert verification.event_count == 3
            assert verification.tail_event_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_event_backed_checkpoint_captures_event_cursor_and_invocation_keys(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Event-backed checkpoint fixture",
                goal="Capture one replay-safe checkpoint anchor",
                idempotency_key=f"checkpoint-anchor-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            created = await control.create(_run(project_id=project.id))
            await control.record_queued_event(
                run_id=created.id,
                event_type=AgentRunEventType.RESEARCH_QUEUED,
                context={},
                artifact_refs=(),
            )
            claim = await control.claim_next(worker_id="worker-a", lease_seconds=60)
            assert claim is not None

            artifact = await ContentAddressedArtifactStore(
                session,
                tmp_path,
            ).put_agent_run_bytes(
                project_id=project.id,
                agent_run_id=created.id,
                basis_hash=created.basis_hash,
                kind="replay_fixture_output",
                content=b"recorded tool response",
                media_type="text/plain",
            )
            recording = InvocationRecording(
                project_id=project.id,
                agent_run_id=created.id,
                producer_attempt_id=claim.lease_token,
                basis_hash=created.basis_hash,
                idempotency_key=f"{created.id}:tool:1",
                request_hash=sha256(b"checkpoint replay invocation").hexdigest(),
                kind=BudgetOperationKind.TOOL,
                provider="fixture-tool",
                operation_name="lookup",
                status=InvocationRecordingStatus.PENDING,
            )
            ledger = InvocationRecordingRepository(session)
            await ledger.prepare(recording)
            recording = await ledger.record(
                recording.model_copy(
                    update={
                        "status": InvocationRecordingStatus.SUCCEEDED,
                        "response_artifact_ref": artifact.ref,
                    }
                )
            )

            admitted = await control.admit_checkpoint(
                claim=claim,
                checkpoint=AdmittedCheckpointRef(
                    thread_id=created.thread_id,
                    checkpoint_id="checkpoint-event-backed",
                    graph_revision="r0",
                    state_schema_version="state-v1",
                    generation=claim.generation,
                ),
            )
            await session.commit()

            assert admitted.admitted_checkpoint is not None
            assert admitted.admitted_checkpoint.event_cursor > 0
            assert admitted.admitted_checkpoint.invocation_recording_keys == (
                recording.idempotency_key,
            )
            events = await PostgresDomainStore(session).list_aggregate_events(
                project.id,
                aggregate_type="agent_run",
                aggregate_id=created.id,
            )
            assert events[-1].event_type == AgentRunEventType.CHECKPOINT_ADMITTED.value
            assert events[-1].project_seq == admitted.admitted_checkpoint.event_cursor
            bundle = await AgentRunReplayBundleService(
                session=session,
                artifact_root=tmp_path,
            ).verify(agent_run_id=created.id)
            assert bundle.status.value == "replayable"
            assert bundle.invocations[0].response_artifact_ref == artifact.ref
            bundle_artifact = await AgentRunReplayBundleService(
                session=session,
                artifact_root=tmp_path,
            ).persist(agent_run_id=created.id)
            assert bundle_artifact.kind == "evaluation_replay_bundle"
            await control.complete(claim=claim, status=AgentRunStatus.SUCCEEDED)
            await session.commit()
            persisted = await AgentRunReplayBundleService(
                session=session,
                artifact_root=tmp_path,
            ).verify_persisted(
                agent_run_id=created.id,
                bundle_ref=bundle_artifact.ref,
            )
            assert persisted == bundle
            with pytest.raises(
                ReplayCheckpointVerifierUnavailableError,
                match="checkpoint verifier",
            ):
                await AgentRunReplayBundleService(
                    session=session,
                    artifact_root=tmp_path,
                ).evaluate_persisted(
                    agent_run_id=created.id,
                    bundle_ref=bundle_artifact.ref,
                )
            checkpoint_reader = _CheckpointReader()
            report = await AgentRunReplayBundleService(
                session=session,
                artifact_root=tmp_path,
                checkpointer=checkpoint_reader,  # type: ignore[arg-type]
            ).evaluate_persisted(agent_run_id=created.id, bundle_ref=bundle_artifact.ref)
            assert report.summary.all_passed
            assert report.cases[0].metric_id == "replay_bundle_readiness"
            assert persisted.admitted_checkpoint is not None
            assert checkpoint_reader.configs == [
                admitted_checkpoint_config(persisted.admitted_checkpoint)
            ]

            (tmp_path / artifact.storage_key).write_bytes(b"corrupted tool response")
            corrupt_bundle = await AgentRunReplayBundleService(
                session=session,
                artifact_root=tmp_path,
            ).build(agent_run_id=created.id)
            assert corrupt_bundle.status.value == "replay_incomplete"
            assert corrupt_bundle.reason_codes == ("response_artifact_integrity_failed",)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_leaves_unsupported_run_queued_and_claims_exact_registered_graph() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Graph Registry control fixture",
                goal="Verify an unsupported AgentRun remains unclaimed",
                idempotency_key=f"graph-registry-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            unsupported = _run(project_id=project.id).model_copy(
                update={"runtime_binding": _binding(graph_revision="r1")}
            )
            supported = _run(project_id=project.id)
            await control.create(unsupported)
            await control.create(supported)
            await session.commit()

        compiled = object()
        worker = LangGraphOrchestrationWorker(
            session_factory=factory,
            registry=GraphRegistry(
                (RegisteredGraph(binding=supported.runtime_binding, compiled_graph=compiled),)
            ),
            worker_id="supported-worker",
        )
        claimed = await worker.claim_once()
        assert claimed is not None
        assert claimed.run.id == supported.id
        assert claimed.compiled_graph is compiled

        async with factory() as session:
            still_queued = await AgentRunControl(session).get(unsupported.id)
            assert still_queued is not None
            assert still_queued.status is AgentRunStatus.QUEUED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_checkpoint_bridge_admits_interrupt_before_releasing_run_to_waiting() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    schema_name = f"aidison_bridge_test_{uuid4().hex}"
    runtime = CheckpointRuntime(
        CheckpointSettings(
            database_url=database_url,
            schema_name=schema_name,
            min_pool_size=1,
            max_pool_size=1,
        )
    )
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Checkpoint bridge fixture",
                goal="Verify a graph interrupt becomes a Control recovery anchor",
                idempotency_key=f"checkpoint-bridge-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            created = await control.create(_run(project_id=project.id))
            await session.commit()
            claim = await control.claim_next(worker_id="bridge-worker", lease_seconds=60)
            assert claim is not None
            await session.commit()

        graph = build_minimal_checkpointed_graph(checkpointer=await runtime.start())
        bridge = LangGraphCheckpointBridge(session_factory=factory)
        interrupted = await bridge.run_to_interrupt(
            claimed=ClaimedGraphRun(
                run=created,
                claim=claim,
                compiled_graph=graph,
            ),
            input_state={"run_id": str(created.id), "prompt": "bridge prompt"},
        )
        assert interrupted.run.status is AgentRunStatus.WAITING
        assert interrupted.run.admitted_checkpoint == interrupted.checkpoint

        resumed = await graph.ainvoke(
            Command(resume="approved"),
            admitted_checkpoint_config(interrupted.checkpoint),
        )
        assert resumed["phase"] == "completed"
        assert resumed["approved_prompt"] == "bridge prompt"
    finally:
        await runtime.close()
        async with await AsyncConnection.connect(
            normalize_psycopg_url(database_url), autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_run_artifact_lineage_is_owned_by_the_agent_run(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="AgentRun artifact fixture",
                goal="Verify raw output is owned by its AgentRun",
                idempotency_key=f"agent-artifact-project-{uuid4()}",
            )
            created = await AgentRunControl(session).create(_run(project_id=project.id))
            metadata = await ContentAddressedArtifactStore(session, tmp_path).put_agent_run_json(
                project_id=project.id,
                agent_run_id=created.id,
                basis_hash=created.basis_hash,
                kind="research_raw_output",
                value={"answer": "raw model payload"},
            )
            assert metadata.agent_run_id == created.id
            replayed = await ContentAddressedArtifactStore(session, tmp_path).read_json_ref(
                project_id=project.id,
                basis_hash=created.basis_hash,
                ref=metadata.ref,
                expected_kind="research_raw_output",
            )
            assert replayed == {"answer": "raw model payload"}
    finally:
        await engine.dispose()
