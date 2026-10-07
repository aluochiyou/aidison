from __future__ import annotations

import os
from datetime import timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text, update

from aidison.application.event_replay import AgentRunEffectShadowProjectionService
from aidison.application.service import ProjectApplication
from aidison.infrastructure.agent_run_effects import (
    AgentRunEffectConflictError,
    AgentRunEffectLedger,
)
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import AgentRunRow
from aidison.infrastructure.replay_snapshots import EventReplaySnapshotRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_run_effect_events import replay_agent_run_effect
from aidison.runtime.agent_run_effects import (
    AgentRunEffectIntent,
    AgentRunEffectState,
    EffectReconciliationOutcome,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, utc_now
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily

pytestmark = pytest.mark.integration


def _run(*, project_id: UUID) -> AgentRun:
    return AgentRun(
        project_id=project_id,
        kind=AgentRunKind.RESEARCH,
        idempotency_key=f"agent-run-effect-{uuid4()}",
        basis_hash=sha256(b"agent-run-effect-basis").hexdigest(),
        basis_project_revision=1,
        runtime_binding=RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="research",
            graph_revision="r3",
            state_schema_version="state-v1",
            profile_binding_ref="profile://research/1",
            policy_binding_ref="policy://research/1",
        ),
        thread_id=f"agent-run-effect-{uuid4()}",
    )


def _intent(*, run: AgentRun) -> AgentRunEffectIntent:
    return AgentRunEffectIntent(
        run_id=run.id,
        task_id=uuid4(),
        basis_hash=run.basis_hash,
        approval_ref=f"effect-approval://{uuid4()}",
        effect_kind="project.publish",
        provider="provider-a",
        external_idempotency_key=f"external-effect-{uuid4()}",
        idempotency_key=f"effect-ledger-{uuid4()}",
        request_hash=sha256(b"effect-request").hexdigest(),
        request_artifact_ref="artifact://effect-request/1",
    )


@pytest.mark.asyncio
async def test_effect_ledger_replays_prepare_and_reconciles_ambiguous_without_redelivery() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_effects CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Effect ledger fixture",
                goal="Verify ambiguous external write is reconciled instead of redelivered",
                idempotency_key=f"effect-ledger-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            run = await control.create(_run(project_id=project.id))
            claim = await control.claim_next(worker_id="effect-worker", lease_seconds=60)
            assert claim is not None
            ledger = AgentRunEffectLedger(session)
            intent = _intent(run=run)
            prepared = await ledger.prepare(intent=intent, claim=claim)
            replayed = await ledger.prepare(intent=intent, claim=claim)
            assert replayed.id == prepared.id

            dispatched = await ledger.mark_dispatched(effect_id=prepared.id, claim=claim)
            ambiguous = await ledger.mark_ambiguous(
                effect_id=prepared.id,
                normalized_error="provider_timeout_after_dispatch",
            )
            reconciled = await ledger.reconcile(
                effect_id=prepared.id,
                outcome=EffectReconciliationOutcome.SUCCEEDED,
                reconciliation_artifact_ref="artifact://effect-reconciliation/1",
            )
            assert dispatched.state is AgentRunEffectState.DISPATCHED
            assert ambiguous.state is AgentRunEffectState.AMBIGUOUS
            assert reconciled.state is AgentRunEffectState.SUCCEEDED
            assert reconciled.reconciled_at is not None
            events = await PostgresDomainStore(session).list_aggregate_events(
                project.id,
                aggregate_type="agent_run_effect",
                aggregate_id=prepared.id,
            )
            assert [event.aggregate_version for event in events] == [1, 2, 3, 4]
            assert replay_agent_run_effect(list(events)).agent_run_effect == reconciled
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_effect_verified_snapshot_tail_replay_preserves_ambiguous_state() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_effects CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()

        async with factory() as session:
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Effect snapshot fixture",
                goal="Verify ambiguous external effect survives snapshot tail replay",
                idempotency_key=f"effect-snapshot-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            run = await control.create(_run(project_id=project.id))
            claim = await control.claim_next(worker_id="effect-worker", lease_seconds=60)
            assert claim is not None
            prepared = await AgentRunEffectLedger(session).prepare(
                intent=_intent(run=run),
                claim=claim,
            )
            await session.commit()

        async with factory() as session:
            service = AgentRunEffectShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                AgentRunEffectLedger(session),
            )
            snapshot = await service.create_verified_snapshot(
                project_id=project.id,
                effect_id=prepared.id,
            )
            duplicate = await service.create_verified_snapshot(
                project_id=project.id,
                effect_id=prepared.id,
            )
            await session.commit()

            assert snapshot.aggregate_version == 1
            assert duplicate == snapshot

        async with factory() as session:
            ledger = AgentRunEffectLedger(session)
            await ledger.mark_dispatched(effect_id=prepared.id, claim=claim)
            ambiguous = await ledger.mark_ambiguous(
                effect_id=prepared.id,
                normalized_error="provider_timeout_after_dispatch",
            )
            await session.commit()
            assert ambiguous.state is AgentRunEffectState.AMBIGUOUS

        async with factory() as session:
            verification = await AgentRunEffectShadowProjectionService(
                PostgresDomainStore(session),
                EventReplaySnapshotRepository(session),
                AgentRunEffectLedger(session),
            ).verify(
                project_id=project.id,
                effect_id=prepared.id,
            )

            assert verification.snapshot == snapshot
            assert verification.snapshot_matches_full_replay
            assert verification.snapshot_tail_hash == verification.full_replay_hash
            assert verification.event_count == 3
            assert verification.tail_event_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_claim_cannot_dispatch_effect_but_may_record_ambiguous_outcome() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE agent_run_effects CASCADE"))
            await session.execute(text("TRUNCATE TABLE agent_runs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Effect ledger stale fixture",
                goal="Verify stale ownership cannot begin an external write",
                idempotency_key=f"effect-ledger-stale-project-{uuid4()}",
            )
            control = AgentRunControl(session)
            run = await control.create(_run(project_id=project.id))
            first_claim = await control.claim_next(worker_id="effect-worker-a", lease_seconds=60)
            assert first_claim is not None
            ledger = AgentRunEffectLedger(session)
            prepared = await ledger.prepare(intent=_intent(run=run), claim=first_claim)
            await ledger.mark_dispatched(effect_id=prepared.id, claim=first_claim)
            await session.execute(
                update(AgentRunRow)
                .where(AgentRunRow.id == run.id)
                .values(lease_expires_at=utc_now() - timedelta(seconds=1))
            )
            second_claim = await control.claim_next(worker_id="effect-worker-b", lease_seconds=60)
            assert second_claim is not None
            with pytest.raises(AgentRunEffectConflictError, match="stale"):
                await ledger.mark_dispatched(effect_id=prepared.id, claim=first_claim)
            assert (
                await ledger.mark_ambiguous(
                    effect_id=prepared.id,
                    normalized_error="worker_lost_before_provider_confirmation",
                )
            ).state is AgentRunEffectState.AMBIGUOUS
            await session.commit()
    finally:
        await engine.dispose()
