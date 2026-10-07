"""Shadow verification for event-backed relational projections.

The first delivery slice covers execution plans only.  It keeps the existing
relation table as the production read model while exercising the exact replay
path needed for a later repair tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.domain.events import (
    EventIntegrityError,
    ExecutionPlanReplaySnapshot,
    ExecutionPlanReplayState,
    StoredDomainEvent,
    canonical_payload_hash,
    execution_plan_replay_state_hash,
    replay_execution_plan,
    replay_execution_plan_from_snapshot,
)
from aidison.domain.models import ExecutionPlanProposal
from aidison.infrastructure.agent_decisions import AgentRunDecisionStore
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_run_effects import AgentRunEffectLedger
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.orm import ExecutionPlanProposalRow
from aidison.infrastructure.replay_snapshots import EventReplaySnapshotRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_result_events import (
    AgentResultReplaySnapshot,
    AgentResultReplayState,
    agent_result_replay_state_hash,
    replay_agent_result,
    replay_agent_result_from_snapshot,
)
from aidison.runtime.agent_run_decision_events import (
    AgentRunDecisionReplaySnapshot,
    AgentRunDecisionReplayState,
    agent_run_decision_replay_state_hash,
    replay_agent_run_decision,
    replay_agent_run_decision_from_snapshot,
)
from aidison.runtime.agent_run_effect_events import (
    AgentRunEffectReplaySnapshot,
    AgentRunEffectReplayState,
    agent_run_effect_replay_state_hash,
    replay_agent_run_effect,
    replay_agent_run_effect_from_snapshot,
)
from aidison.runtime.agent_run_events import (
    AgentRunReplaySnapshot,
    AgentRunReplayState,
    agent_run_replay_state_hash,
    replay_agent_run,
    replay_agent_run_from_snapshot,
)
from aidison.runtime.agent_runs import AgentRun


class ProjectionMismatchError(EventIntegrityError):
    """Event replay and the canonical relation table describe different state."""


class ProjectionRepairConflictError(ProjectionMismatchError):
    """A repair preview became stale, or the relation row cannot be repaired safely."""


@dataclass(frozen=True)
class ExecutionPlanProjectionRepairPreview:
    """Read-only comparison a human or maintenance command must approve explicitly."""

    project_id: UUID
    execution_plan_id: UUID
    event_cursor: int
    event_count: int
    current_relation_hash: str | None
    replayed_relation_hash: str
    repair_required: bool


@dataclass(frozen=True)
class ExecutionPlanProjectionRepairResult:
    """The preview plus an explicit indication of whether a relation row changed."""

    preview: ExecutionPlanProjectionRepairPreview
    applied: bool


@dataclass(frozen=True)
class ExecutionPlanProjectionVerification:
    """Evidence that full and accelerated replay agree with the read model."""

    snapshot: ExecutionPlanReplaySnapshot | None
    full_replay_hash: str
    snapshot_tail_hash: str | None
    relational_hash: str
    event_count: int
    tail_event_count: int

    @property
    def snapshot_matches_full_replay(self) -> bool:
        return self.snapshot_tail_hash in {None, self.full_replay_hash}


class ExecutionPlanShadowProjectionService:
    """Verify execution-plan replay before persisting any acceleration point."""

    def __init__(
        self,
        domain_store: PostgresDomainStore,
        snapshot_repository: EventReplaySnapshotRepository,
    ) -> None:
        self._domain_store = domain_store
        self._snapshots = snapshot_repository

    async def create_verified_snapshot(
        self,
        *,
        project_id: UUID,
        execution_plan_id: UUID,
    ) -> ExecutionPlanReplaySnapshot:
        """Persist a snapshot only after from-zero replay matches the read model."""
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            execution_plan_id=execution_plan_id,
        )
        snapshot = ExecutionPlanReplaySnapshot.from_state(
            state,
            project_seq=events[-1].project_seq,
        )
        return await self._snapshots.persist_execution_plan(snapshot)

    async def verify(
        self,
        *,
        project_id: UUID,
        execution_plan_id: UUID,
    ) -> ExecutionPlanProjectionVerification:
        """Compare relation state, full replay and the latest snapshot-tail path."""
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            execution_plan_id=execution_plan_id,
        )
        full_hash = execution_plan_replay_state_hash(state)
        relational_hash = _execution_plan_proposal_hash(state.proposal)
        snapshot = await self._snapshots.get_latest_execution_plan(
            project_id=project_id,
            aggregate_id=execution_plan_id,
        )
        if snapshot is None:
            return ExecutionPlanProjectionVerification(
                snapshot=None,
                full_replay_hash=full_hash,
                snapshot_tail_hash=None,
                relational_hash=relational_hash,
                event_count=len(events),
                tail_event_count=0,
            )

        tail = [event for event in events if event.project_seq > snapshot.project_seq]
        accelerated_state = replay_execution_plan_from_snapshot(snapshot, tail)
        accelerated_hash = execution_plan_replay_state_hash(accelerated_state)
        if accelerated_hash != full_hash:
            raise ProjectionMismatchError(
                "execution-plan snapshot-tail replay differs from full replay"
            )
        return ExecutionPlanProjectionVerification(
            snapshot=snapshot,
            full_replay_hash=full_hash,
            snapshot_tail_hash=accelerated_hash,
            relational_hash=relational_hash,
            event_count=len(events),
            tail_event_count=len(tail),
        )

    async def _from_zero_and_relation_match(
        self,
        *,
        project_id: UUID,
        execution_plan_id: UUID,
    ) -> tuple[ExecutionPlanReplayState, list[StoredDomainEvent]]:
        events = list(
            await self._domain_store.list_aggregate_events(
                project_id,
                aggregate_type="execution_plan",
                aggregate_id=execution_plan_id,
            )
        )
        state = replay_execution_plan(events)
        stored = await self._domain_store.get_execution_plan_proposal(execution_plan_id)
        if stored is None:
            raise ProjectionMismatchError("execution-plan relation is missing")
        if stored != state.proposal:
            raise ProjectionMismatchError(
                "execution-plan relation differs from deterministic event replay"
            )
        return state, events


def _execution_plan_proposal_hash(proposal: ExecutionPlanProposal) -> str:
    """Use relation-shaped state so it is comparable with replayed proposal state."""
    return canonical_payload_hash(proposal.model_dump(mode="json"))


class ExecutionPlanProjectionRepairService:
    """Explicitly repair one ExecutionPlan read-model row from its event stream.

    This is intentionally a maintenance boundary, not a normal request path.
    It never mutates ``domain_events`` or creates a new business event.  The
    relation remains the normal online read model; this service only restores
    it after an operator has examined a preview and supplied its exact current
    relation hash back as an optimistic precondition.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._domain_store = PostgresDomainStore(session)

    async def preview(
        self,
        *,
        project_id: UUID,
        execution_plan_id: UUID,
    ) -> ExecutionPlanProjectionRepairPreview:
        """Rebuild the target projection without acquiring a write lock or mutating data."""

        events = await self._events(project_id=project_id, execution_plan_id=execution_plan_id)
        state = replay_execution_plan(events)
        row = await self._session.get(ExecutionPlanProposalRow, execution_plan_id)
        current_hash = _execution_plan_relation_row_hash(row) if row is not None else None
        target_hash = _execution_plan_relation_hash(state.proposal)
        return ExecutionPlanProjectionRepairPreview(
            project_id=project_id,
            execution_plan_id=execution_plan_id,
            event_cursor=events[-1].project_seq,
            event_count=len(events),
            current_relation_hash=current_hash,
            replayed_relation_hash=target_hash,
            repair_required=current_hash != target_hash,
        )

    async def apply(
        self,
        *,
        project_id: UUID,
        execution_plan_id: UUID,
        expected_current_relation_hash: str,
        dry_run: bool = True,
    ) -> ExecutionPlanProjectionRepairResult:
        """Apply a reviewed repair under lock, or return its locked dry-run result.

        Missing rows are not auto-created.  Their absence can indicate a failed
        original command rather than a damaged projection, so they require a
        separately audited restoration path.
        """

        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"projection-repair:execution-plan:{execution_plan_id}"},
        )
        row = await self._session.scalar(
            select(ExecutionPlanProposalRow)
            .where(
                ExecutionPlanProposalRow.id == execution_plan_id,
                ExecutionPlanProposalRow.project_id == project_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ProjectionRepairConflictError(
                "execution-plan relation is missing; automatic row creation is forbidden"
            )
        current_hash = _execution_plan_relation_row_hash(row)
        if current_hash != expected_current_relation_hash:
            raise ProjectionRepairConflictError(
                "execution-plan relation changed after repair preview; request a new preview"
            )

        events = await self._events(project_id=project_id, execution_plan_id=execution_plan_id)
        state = replay_execution_plan(events)
        target = state.proposal
        target_hash = _execution_plan_relation_hash(target)
        preview = ExecutionPlanProjectionRepairPreview(
            project_id=project_id,
            execution_plan_id=execution_plan_id,
            event_cursor=events[-1].project_seq,
            event_count=len(events),
            current_relation_hash=current_hash,
            replayed_relation_hash=target_hash,
            repair_required=current_hash != target_hash,
        )
        if dry_run or not preview.repair_required:
            return ExecutionPlanProjectionRepairResult(preview=preview, applied=False)

        row.status = target.status.value
        row.basis_hash = target.basis_hash
        row.scope_hash = target.scope_hash or ""
        row.payload = target.model_dump(mode="json")
        await self._session.flush()
        return ExecutionPlanProjectionRepairResult(preview=preview, applied=True)

    async def _events(
        self,
        *,
        project_id: UUID,
        execution_plan_id: UUID,
    ) -> list[StoredDomainEvent]:
        events = list(
            await self._domain_store.list_aggregate_events(
                project_id,
                aggregate_type="execution_plan",
                aggregate_id=execution_plan_id,
            )
        )
        if not events:
            raise ProjectionRepairConflictError("execution-plan event stream is missing")
        return events


def _execution_plan_relation_row_hash(row: ExecutionPlanProposalRow) -> str:
    """Hash every physical projection column so typed-column corruption is repairable too."""

    return canonical_payload_hash(
        {
            "id": str(row.id),
            "project_id": str(row.project_id),
            "status": row.status,
            "basis_hash": row.basis_hash,
            "scope_hash": row.scope_hash,
            "payload": row.payload,
        }
    )


def _execution_plan_relation_hash(proposal: ExecutionPlanProposal) -> str:
    """Hash the exact physical-row shape expected from replayed plan state."""

    return canonical_payload_hash(
        {
            "id": str(proposal.id),
            "project_id": str(proposal.project_id),
            "status": proposal.status.value,
            "basis_hash": proposal.basis_hash,
            "scope_hash": proposal.scope_hash or "",
            "payload": proposal.model_dump(mode="json"),
        }
    )


@dataclass(frozen=True)
class AgentRunProjectionVerification:
    """Evidence that AgentRun control state matches full and accelerated replay."""

    snapshot: AgentRunReplaySnapshot | None
    full_replay_hash: str
    snapshot_tail_hash: str | None
    relational_hash: str
    event_count: int
    tail_event_count: int

    @property
    def snapshot_matches_full_replay(self) -> bool:
        return self.snapshot_tail_hash in {None, self.full_replay_hash}


class AgentRunShadowProjectionService:
    """Verify AgentRun lifecycle replay without turning snapshots into runtime state.

    The service intentionally compares the replayed control state with the
    current AgentRun relation before it persists an acceleration point. It does
    not read or write LangGraph checkpoint tables.
    """

    def __init__(
        self,
        domain_store: PostgresDomainStore,
        snapshot_repository: EventReplaySnapshotRepository,
        agent_run_control: AgentRunControl,
    ) -> None:
        self._domain_store = domain_store
        self._snapshots = snapshot_repository
        self._agent_runs = agent_run_control

    async def create_verified_snapshot(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
    ) -> AgentRunReplaySnapshot:
        """Persist only a lifecycle state independently rebuilt from its event stream."""
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            agent_run_id=agent_run_id,
        )
        snapshot = AgentRunReplaySnapshot.from_state(
            state,
            project_seq=events[-1].project_seq,
            created_at=events[-1].recorded_at,
        )
        return await self._snapshots.persist_agent_run(snapshot)

    async def verify(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
    ) -> AgentRunProjectionVerification:
        """Compare relation state, full replay and any available snapshot-tail path."""
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            agent_run_id=agent_run_id,
        )
        full_hash = agent_run_replay_state_hash(state)
        relational_hash = _agent_run_hash(state.agent_run)
        snapshot = await self._snapshots.get_latest_agent_run(
            project_id=project_id,
            aggregate_id=agent_run_id,
        )
        if snapshot is None:
            return AgentRunProjectionVerification(
                snapshot=None,
                full_replay_hash=full_hash,
                snapshot_tail_hash=None,
                relational_hash=relational_hash,
                event_count=len(events),
                tail_event_count=0,
            )

        tail = [event for event in events if event.project_seq > snapshot.project_seq]
        accelerated_state = replay_agent_run_from_snapshot(snapshot, tail)
        accelerated_hash = agent_run_replay_state_hash(accelerated_state)
        if accelerated_hash != full_hash:
            raise ProjectionMismatchError("AgentRun snapshot-tail replay differs from full replay")
        return AgentRunProjectionVerification(
            snapshot=snapshot,
            full_replay_hash=full_hash,
            snapshot_tail_hash=accelerated_hash,
            relational_hash=relational_hash,
            event_count=len(events),
            tail_event_count=len(tail),
        )

    async def _from_zero_and_relation_match(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
    ) -> tuple[AgentRunReplayState, list[StoredDomainEvent]]:
        events = list(
            await self._domain_store.list_aggregate_events(
                project_id,
                aggregate_type="agent_run",
                aggregate_id=agent_run_id,
            )
        )
        state = replay_agent_run(events)
        stored = await self._agent_runs.get(agent_run_id)
        if stored is None:
            raise ProjectionMismatchError("AgentRun relation is missing")
        if stored != state.agent_run:
            raise ProjectionMismatchError(
                "AgentRun relation differs from deterministic event replay"
            )
        return state, events


def _agent_run_hash(run: AgentRun) -> str:
    return canonical_payload_hash(run.model_dump(mode="json"))


@dataclass(frozen=True)
class AgentResultProjectionVerification:
    """Evidence that a producer result and its verdict can be replayed safely."""

    snapshot: AgentResultReplaySnapshot | None
    full_replay_hash: str
    snapshot_tail_hash: str | None
    relational_hash: str
    event_count: int
    tail_event_count: int

    @property
    def snapshot_matches_full_replay(self) -> bool:
        return self.snapshot_tail_hash in {None, self.full_replay_hash}


class AgentResultShadowProjectionService:
    """Shadow-verify ResultEnvelope/admission state before retaining a snapshot.

    A result row alone is not a dependency-unlock signal. The comparison keeps
    its separate AdmissionRecord explicit, so no caller can accidentally treat
    a recorded or quarantined result as accepted during accelerated replay.
    """

    def __init__(
        self,
        domain_store: PostgresDomainStore,
        snapshot_repository: EventReplaySnapshotRepository,
        result_store: AgentResultStore,
    ) -> None:
        self._domain_store = domain_store
        self._snapshots = snapshot_repository
        self._results = result_store

    async def create_verified_snapshot(
        self,
        *,
        project_id: UUID,
        result_id: UUID,
    ) -> AgentResultReplaySnapshot:
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            result_id=result_id,
        )
        snapshot = AgentResultReplaySnapshot.from_state(
            state,
            project_seq=events[-1].project_seq,
            created_at=events[-1].recorded_at,
        )
        return await self._snapshots.persist_agent_result(snapshot)

    async def verify(
        self,
        *,
        project_id: UUID,
        result_id: UUID,
    ) -> AgentResultProjectionVerification:
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            result_id=result_id,
        )
        full_hash = agent_result_replay_state_hash(state)
        relational_hash = _agent_result_state_hash(state)
        snapshot = await self._snapshots.get_latest_agent_result(
            project_id=project_id,
            aggregate_id=result_id,
        )
        if snapshot is None:
            return AgentResultProjectionVerification(
                snapshot=None,
                full_replay_hash=full_hash,
                snapshot_tail_hash=None,
                relational_hash=relational_hash,
                event_count=len(events),
                tail_event_count=0,
            )
        tail = [event for event in events if event.project_seq > snapshot.project_seq]
        accelerated_state = replay_agent_result_from_snapshot(snapshot, tail)
        accelerated_hash = agent_result_replay_state_hash(accelerated_state)
        if accelerated_hash != full_hash:
            raise ProjectionMismatchError(
                "AgentRun result snapshot-tail replay differs from full replay"
            )
        return AgentResultProjectionVerification(
            snapshot=snapshot,
            full_replay_hash=full_hash,
            snapshot_tail_hash=accelerated_hash,
            relational_hash=relational_hash,
            event_count=len(events),
            tail_event_count=len(tail),
        )

    async def _from_zero_and_relation_match(
        self,
        *,
        project_id: UUID,
        result_id: UUID,
    ) -> tuple[AgentResultReplayState, list[StoredDomainEvent]]:
        events = list(
            await self._domain_store.list_aggregate_events(
                project_id,
                aggregate_type="agent_run_result",
                aggregate_id=result_id,
            )
        )
        state = replay_agent_result(events)
        stored_result = await self._results.get_result(result_id=result_id)
        stored_admission = await self._results.get_admission(result_id=result_id)
        if stored_result != state.result_envelope or stored_admission != state.admission_record:
            raise ProjectionMismatchError(
                "AgentRun result relation differs from deterministic event replay"
            )
        return state, events


def _agent_result_state_hash(state: AgentResultReplayState) -> str:
    return canonical_payload_hash(
        {
            "result": state.result_envelope.model_dump(mode="json"),
            "admission": (
                state.admission_record.model_dump(mode="json")
                if state.admission_record is not None
                else None
            ),
        }
    )


@dataclass(frozen=True)
class AgentRunDecisionProjectionVerification:
    """Evidence that one Proposal review and its user verdict replay correctly."""

    snapshot: AgentRunDecisionReplaySnapshot | None
    full_replay_hash: str
    snapshot_tail_hash: str | None
    relational_hash: str
    event_count: int
    tail_event_count: int

    @property
    def snapshot_matches_full_replay(self) -> bool:
        return self.snapshot_tail_hash in {None, self.full_replay_hash}


class AgentRunDecisionShadowProjectionService:
    """Shadow-verify one pending/resolved Proposal review before snapshotting it."""

    def __init__(
        self,
        domain_store: PostgresDomainStore,
        snapshot_repository: EventReplaySnapshotRepository,
        decision_store: AgentRunDecisionStore,
    ) -> None:
        self._domain_store = domain_store
        self._snapshots = snapshot_repository
        self._decisions = decision_store

    async def create_verified_snapshot(
        self,
        *,
        project_id: UUID,
        decision_id: UUID,
    ) -> AgentRunDecisionReplaySnapshot:
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            decision_id=decision_id,
        )
        snapshot = AgentRunDecisionReplaySnapshot.from_state(
            state,
            project_seq=events[-1].project_seq,
            created_at=events[-1].recorded_at,
        )
        return await self._snapshots.persist_agent_run_decision(snapshot)

    async def verify(
        self,
        *,
        project_id: UUID,
        decision_id: UUID,
    ) -> AgentRunDecisionProjectionVerification:
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            decision_id=decision_id,
        )
        full_hash = agent_run_decision_replay_state_hash(state)
        relational_hash = canonical_payload_hash(
            state.agent_run_decision.model_dump(mode="json")
        )
        snapshot = await self._snapshots.get_latest_agent_run_decision(
            project_id=project_id,
            aggregate_id=decision_id,
        )
        if snapshot is None:
            return AgentRunDecisionProjectionVerification(
                snapshot=None,
                full_replay_hash=full_hash,
                snapshot_tail_hash=None,
                relational_hash=relational_hash,
                event_count=len(events),
                tail_event_count=0,
            )
        tail = [event for event in events if event.project_seq > snapshot.project_seq]
        accelerated_state = replay_agent_run_decision_from_snapshot(snapshot, tail)
        accelerated_hash = agent_run_decision_replay_state_hash(accelerated_state)
        if accelerated_hash != full_hash:
            raise ProjectionMismatchError(
                "AgentRun decision snapshot-tail replay differs from full replay"
            )
        return AgentRunDecisionProjectionVerification(
            snapshot=snapshot,
            full_replay_hash=full_hash,
            snapshot_tail_hash=accelerated_hash,
            relational_hash=relational_hash,
            event_count=len(events),
            tail_event_count=len(tail),
        )

    async def _from_zero_and_relation_match(
        self,
        *,
        project_id: UUID,
        decision_id: UUID,
    ) -> tuple[AgentRunDecisionReplayState, list[StoredDomainEvent]]:
        events = list(
            await self._domain_store.list_aggregate_events(
                project_id,
                aggregate_type="agent_run_decision",
                aggregate_id=decision_id,
            )
        )
        state = replay_agent_run_decision(events)
        stored = await self._decisions.get(decision_id)
        if stored != state.agent_run_decision:
            raise ProjectionMismatchError(
                "AgentRun decision relation differs from deterministic event replay"
            )
        return state, events


@dataclass(frozen=True)
class AgentRunEffectProjectionVerification:
    """Evidence that an external-effect ledger remains safe under accelerated replay."""

    snapshot: AgentRunEffectReplaySnapshot | None
    full_replay_hash: str
    snapshot_tail_hash: str | None
    relational_hash: str
    event_count: int
    tail_event_count: int

    @property
    def snapshot_matches_full_replay(self) -> bool:
        return self.snapshot_tail_hash in {None, self.full_replay_hash}


class AgentRunEffectShadowProjectionService:
    """Shadow-verify effects without changing their dispatch or reconciliation path."""

    def __init__(
        self,
        domain_store: PostgresDomainStore,
        snapshot_repository: EventReplaySnapshotRepository,
        effect_ledger: AgentRunEffectLedger,
    ) -> None:
        self._domain_store = domain_store
        self._snapshots = snapshot_repository
        self._effects = effect_ledger

    async def create_verified_snapshot(
        self,
        *,
        project_id: UUID,
        effect_id: UUID,
    ) -> AgentRunEffectReplaySnapshot:
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            effect_id=effect_id,
        )
        snapshot = AgentRunEffectReplaySnapshot.from_state(
            state,
            project_seq=events[-1].project_seq,
            created_at=events[-1].recorded_at,
        )
        return await self._snapshots.persist_agent_run_effect(snapshot)

    async def verify(
        self,
        *,
        project_id: UUID,
        effect_id: UUID,
    ) -> AgentRunEffectProjectionVerification:
        state, events = await self._from_zero_and_relation_match(
            project_id=project_id,
            effect_id=effect_id,
        )
        full_hash = agent_run_effect_replay_state_hash(state)
        relational_hash = canonical_payload_hash(state.agent_run_effect.model_dump(mode="json"))
        snapshot = await self._snapshots.get_latest_agent_run_effect(
            project_id=project_id,
            aggregate_id=effect_id,
        )
        if snapshot is None:
            return AgentRunEffectProjectionVerification(
                snapshot=None,
                full_replay_hash=full_hash,
                snapshot_tail_hash=None,
                relational_hash=relational_hash,
                event_count=len(events),
                tail_event_count=0,
            )
        tail = [event for event in events if event.project_seq > snapshot.project_seq]
        accelerated_state = replay_agent_run_effect_from_snapshot(snapshot, tail)
        accelerated_hash = agent_run_effect_replay_state_hash(accelerated_state)
        if accelerated_hash != full_hash:
            raise ProjectionMismatchError(
                "AgentRun effect snapshot-tail replay differs from full replay"
            )
        return AgentRunEffectProjectionVerification(
            snapshot=snapshot,
            full_replay_hash=full_hash,
            snapshot_tail_hash=accelerated_hash,
            relational_hash=relational_hash,
            event_count=len(events),
            tail_event_count=len(tail),
        )

    async def _from_zero_and_relation_match(
        self,
        *,
        project_id: UUID,
        effect_id: UUID,
    ) -> tuple[AgentRunEffectReplayState, list[StoredDomainEvent]]:
        events = list(
            await self._domain_store.list_aggregate_events(
                project_id,
                aggregate_type="agent_run_effect",
                aggregate_id=effect_id,
            )
        )
        state = replay_agent_run_effect(events)
        stored = await self._effects.get(effect_id)
        if stored != state.agent_run_effect:
            raise ProjectionMismatchError(
                "AgentRun effect relation differs from deterministic event replay"
            )
        return state, events
