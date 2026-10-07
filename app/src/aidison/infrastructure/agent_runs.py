"""PostgreSQL control operations for LangGraph-owned AgentRuns."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import AgentRunRow, InvocationRecordingRow, ProjectRow
from aidison.runtime.agent_run_events import (
    AgentRunEventType,
    agent_run_lifecycle_metadata,
)
from aidison.runtime.agent_runs import (
    AdmittedCheckpointRef,
    AgentRun,
    AgentRunClaim,
    AgentRunStatus,
    utc_now,
)
from aidison.runtime.identity import (
    RuntimeBinding,
    UnsupportedRuntimeBinding,
    WorkerRuntimeSupport,
    ensure_worker_supports,
)


class AgentRunControlError(RuntimeError):
    """Base error for thin AgentRun control operations."""


class AgentRunNotFoundError(AgentRunControlError):
    """Raised when the requested AgentRun does not exist."""


class AgentRunConflictError(AgentRunControlError):
    """Raised when stale ownership or incompatible idempotency is detected."""


def _terminal_event_type(status: AgentRunStatus) -> AgentRunEventType:
    return {
        AgentRunStatus.SUCCEEDED: AgentRunEventType.SUCCEEDED,
        AgentRunStatus.FAILED: AgentRunEventType.FAILED,
        AgentRunStatus.CANCELLED: AgentRunEventType.CANCELLED,
    }[status]


class AgentRunControl:
    """Lease and checkpoint admission only; graph state remains in LangGraph."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, run: AgentRun) -> AgentRun:
        existing = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.idempotency_key == run.idempotency_key)
        )
        if existing is not None:
            restored = self._from_row(existing)
            if not self._same_create_request(restored, run):
                raise AgentRunConflictError("AgentRun idempotency key has another payload")
            return restored

        self._session.add(
            AgentRunRow(
                id=run.id,
                project_id=run.project_id,
                kind=run.kind.value,
                idempotency_key=run.idempotency_key,
                basis_hash=run.basis_hash,
                basis_project_revision=run.basis_project_revision,
                runtime_binding=run.runtime_binding.model_dump(mode="json"),
                thread_id=run.thread_id,
                run_contract_ref=run.run_contract_ref,
                coverage_contract_ref=run.coverage_contract_ref,
                status=run.status.value,
                cancel_requested=run.cancel_requested,
                current_generation=run.current_generation,
                lifecycle_event_version=0,
                admitted_checkpoint=(
                    run.admitted_checkpoint.model_dump(mode="json")
                    if run.admitted_checkpoint is not None
                    else None
                ),
                created_at=run.created_at,
                started_at=run.started_at,
                updated_at=run.updated_at,
                completed_at=run.completed_at,
            )
        )
        await self._session.flush()
        return run

    async def get(self, run_id: UUID) -> AgentRun | None:
        row = await self._session.get(AgentRunRow, run_id)
        return self._from_row(row) if row is not None else None

    async def bind_coverage_contract(
        self,
        *,
        run_id: UUID,
        coverage_contract_ref: str,
    ) -> AgentRun:
        """Bind one immutable coverage artifact before the Research graph starts."""

        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == run_id).with_for_update()
        )
        if row is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if row.coverage_contract_ref is not None:
            if row.coverage_contract_ref != coverage_contract_ref:
                raise AgentRunConflictError("AgentRun already has another coverage contract")
            return self._from_row(row)
        if row.status != AgentRunStatus.QUEUED.value:
            raise AgentRunConflictError("coverage contract can only bind before AgentRun starts")
        row.coverage_contract_ref = coverage_contract_ref
        row.updated_at = utc_now()
        await self._session.flush()
        return self._from_row(row)

    async def bind_run_contract(
        self,
        *,
        run_id: UUID,
        run_contract_ref: str,
    ) -> AgentRun:
        """Bind the one immutable run input contract before graph execution starts."""

        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == run_id).with_for_update()
        )
        if row is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if row.run_contract_ref is not None:
            if row.run_contract_ref != run_contract_ref:
                raise AgentRunConflictError("AgentRun already has another run contract")
            return self._from_row(row)
        if row.status != AgentRunStatus.QUEUED.value:
            raise AgentRunConflictError("run contract can only bind before AgentRun starts")
        row.run_contract_ref = run_contract_ref
        row.updated_at = utc_now()
        await self._session.flush()
        return self._from_row(row)

    async def record_queued_event(
        self,
        *,
        run_id: UUID,
        event_type: AgentRunEventType,
        context: dict[str, object],
        artifact_refs: tuple[str, ...],
    ) -> AgentRun:
        """Atomically establish the versioned lifecycle stream for one Run.

        A Run is only event-backed after its immutable contracts have been
        bound. This prevents replay from observing a queued Run whose tool,
        coverage or budget authorization is still incomplete.
        """

        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == run_id).with_for_update()
        )
        if row is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if row.lifecycle_event_version > 0:
            return self._from_row(row)
        if row.status != AgentRunStatus.QUEUED.value or row.current_generation != 0:
            raise AgentRunConflictError("only a new queued AgentRun can establish its event stream")
        row.lifecycle_event_version = 1
        row.updated_at = utc_now()
        await self._session.flush()
        run = self._from_row(row)
        payload: dict[str, object] = {"run": run.model_dump(mode="json"), **context}
        metadata = agent_run_lifecycle_metadata(
            run=run,
            aggregate_version=1,
            payload=payload,
            artifact_refs=artifact_refs,
        )
        from aidison.infrastructure.store import PostgresDomainStore

        await PostgresDomainStore(self._session).append_event(
            row.project_id,
            event_type.value,
            payload,
            metadata,
        )
        return run

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        support: WorkerRuntimeSupport | None = None,
        run_id: UUID | None = None,
    ) -> AgentRunClaim | None:
        """Claim one runnable Run this worker explicitly supports.

        ``run_id`` is an operator-only targeting constraint for safe diagnosis;
        it never bypasses queued/running, lease, cancellation, or binding
        checks. Without it, the earliest supported Run is claimed.
        """

        now = utc_now()
        runnable = or_(
            AgentRunRow.status == AgentRunStatus.QUEUED.value,
            (AgentRunRow.status == AgentRunStatus.RUNNING.value)
            & (AgentRunRow.lease_expires_at < now),
        )
        statement = select(AgentRunRow).where(AgentRunRow.cancel_requested.is_(False), runnable)
        if run_id is not None:
            statement = statement.where(AgentRunRow.id == run_id)
        candidates = list(
            (
                await self._session.scalars(
                    statement.order_by(AgentRunRow.created_at, AgentRunRow.id).limit(64)
                )
            ).all()
        )
        for candidate_hint in candidates:
            if support is not None:
                try:
                    ensure_worker_supports(
                        binding=self._from_row(candidate_hint).runtime_binding,
                        support=support,
                    )
                except UnsupportedRuntimeBinding:
                    continue
            candidate = await self._session.scalar(
                select(AgentRunRow)
                .where(
                    AgentRunRow.id == candidate_hint.id,
                    AgentRunRow.cancel_requested.is_(False),
                    runnable,
                )
                .with_for_update(skip_locked=True)
            )
            if candidate is None:
                continue
            if support is not None:
                try:
                    ensure_worker_supports(
                        binding=self._from_row(candidate).runtime_binding,
                        support=support,
                    )
                except UnsupportedRuntimeBinding:
                    continue
            break
        else:
            return None

        generation = candidate.current_generation + 1
        lease_token = uuid4()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        candidate.status = AgentRunStatus.RUNNING.value
        if candidate.started_at is None:
            candidate.started_at = now
        candidate.current_generation = generation
        candidate.lease_owner = worker_id
        candidate.lease_token = lease_token
        candidate.lease_expires_at = lease_expires_at
        candidate.updated_at = now
        await self._session.flush()
        await self._append_lifecycle_event(candidate, AgentRunEventType.RUNNING)
        return AgentRunClaim(
            run_id=candidate.id,
            worker_id=worker_id,
            generation=generation,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
        )

    async def admit_checkpoint(
        self,
        *,
        claim: AgentRunClaim,
        checkpoint: AdmittedCheckpointRef,
    ) -> AgentRun:
        row = await self._locked_active_row(claim)
        if checkpoint.generation != claim.generation:
            raise AgentRunConflictError("checkpoint generation does not match AgentRun claim")
        if checkpoint.thread_id != row.thread_id:
            raise AgentRunConflictError("checkpoint thread does not match the AgentRun")
        binding = RuntimeBinding.model_validate(row.runtime_binding)
        if (
            checkpoint.graph_revision != binding.graph_revision
            or checkpoint.state_schema_version != binding.state_schema_version
        ):
            raise AgentRunConflictError(
                "checkpoint runtime binding does not match the AgentRun"
            )
        admitted = checkpoint
        if row.lifecycle_event_version > 0:
            if checkpoint.event_cursor != 0 or checkpoint.invocation_recording_keys:
                raise AgentRunConflictError(
                    "event-backed checkpoint anchor must be captured by run control"
                )
            project = await self._session.scalar(
                select(ProjectRow).where(ProjectRow.id == row.project_id).with_for_update()
            )
            if project is None:  # pragma: no cover - AgentRun has a project FK
                raise AgentRunNotFoundError("AgentRun project not found")
            recording_keys = tuple(
                (
                    await self._session.scalars(
                        select(InvocationRecordingRow.idempotency_key)
                        .where(
                            InvocationRecordingRow.project_id == row.project_id,
                            InvocationRecordingRow.agent_run_id == row.id,
                        )
                        .order_by(
                            InvocationRecordingRow.created_at,
                            InvocationRecordingRow.id,
                        )
                    )
                ).all()
            )
            # Holding the Project row lock makes the checkpoint lifecycle event
            # below the next project sequence.  The event payload and anchor
            # therefore agree on one durable cursor without copying GraphState.
            admitted = checkpoint.model_copy(
                update={
                    "event_cursor": project.event_sequence + 1,
                    "invocation_recording_keys": recording_keys,
                }
            )
        row.admitted_checkpoint = admitted.model_dump(mode="json")
        row.updated_at = utc_now()
        await self._session.flush()
        event_cursor = await self._append_lifecycle_event(
            row, AgentRunEventType.CHECKPOINT_ADMITTED
        )
        if admitted.event_cursor and event_cursor != admitted.event_cursor:
            raise AgentRunConflictError("checkpoint event cursor was not admitted atomically")
        return self._from_row(row)

    async def renew_claim(
        self,
        *,
        claim: AgentRunClaim,
        lease_seconds: int,
    ) -> AgentRunClaim:
        """Extend the current lease while a long-running graph is active.

        Renewal is fenced by the same generation and lease token as every
        other control mutation. A takeover therefore cannot be prolonged by
        an old worker, while a healthy worker can safely outlive the initial
        lease window during model/search calls.
        """

        row = await self._locked_active_row(claim)
        renewed_until = utc_now() + timedelta(seconds=lease_seconds)
        row.lease_expires_at = renewed_until
        row.updated_at = utc_now()
        await self._session.flush()
        return claim.model_copy(update={"lease_expires_at": renewed_until})

    async def wait_for_decision(self, *, claim: AgentRunClaim) -> AgentRun:
        """Persist an interrupt boundary and release the active worker lease.

        A later Decision bridge may move this run back to ``queued``.  This
        control operation does not try to resume a graph or interpret a human
        answer; LangGraph owns that state and its dedicated checkpoint tables.
        """

        row = await self._locked_active_row(claim)
        now = utc_now()
        row.status = AgentRunStatus.WAITING.value
        row.lease_owner = None
        row.lease_token = None
        row.lease_expires_at = None
        row.updated_at = now
        await self._session.flush()
        await self._append_lifecycle_event(row, AgentRunEventType.WAITING)
        return self._from_row(row)

    async def resume_after_pause(self, *, run_id: UUID) -> AgentRun:
        """Requeue a Run stopped at the pre-dispatch pause safe point.

        This deliberately does not resume a LangGraph interrupt.  Decision
        resume owns an admitted checkpoint and has a separate bridge.  The
        caller must first verify that this waiting Run came from an
        acknowledged pause request.
        """

        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == run_id).with_for_update()
        )
        if row is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if row.status == AgentRunStatus.QUEUED.value:
            return self._from_row(row)
        if row.status != AgentRunStatus.WAITING.value:
            raise AgentRunConflictError("only a paused waiting AgentRun can be requeued")
        if row.admitted_checkpoint is not None:
            raise AgentRunConflictError(
                "checkpointed AgentRun must resume through its decision bridge"
            )
        row.status = AgentRunStatus.QUEUED.value
        row.updated_at = utc_now()
        await self._session.flush()
        await self._append_lifecycle_event(row, AgentRunEventType.REQUEUED)
        return self._from_row(row)

    async def complete(
        self,
        *,
        claim: AgentRunClaim,
        status: AgentRunStatus,
    ) -> AgentRun:
        """Finish a claimed run with an explicit terminal outcome."""

        if status not in {
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            raise ValueError("AgentRun completion requires a terminal status")
        row = await self._locked_active_row(claim)
        now = utc_now()
        row.status = status.value
        row.lease_owner = None
        row.lease_token = None
        row.lease_expires_at = None
        row.updated_at = now
        row.completed_at = now
        await self._session.flush()
        await self._append_lifecycle_event(row, _terminal_event_type(status))
        return self._from_row(row)

    async def ensure_active_claim(self, *, claim: AgentRunClaim) -> AgentRun:
        """Recheck a lease before an external result can enter Control state."""

        return self._from_row(await self._locked_active_row(claim))

    async def complete_waiting_run(self, *, run_id: UUID, succeeded: bool) -> AgentRun:
        """Terminal transition after a durable user decision; no worker lease exists."""

        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == run_id).with_for_update()
        )
        if row is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if row.status != AgentRunStatus.WAITING.value:
            raise AgentRunConflictError("only a waiting AgentRun can complete from user decision")
        now = utc_now()
        row.status = (AgentRunStatus.SUCCEEDED if succeeded else AgentRunStatus.CANCELLED).value
        row.updated_at = now
        row.completed_at = now
        await self._session.flush()
        await self._append_lifecycle_event(
            row,
            AgentRunEventType.SUCCEEDED if succeeded else AgentRunEventType.CANCELLED,
        )
        return self._from_row(row)

    async def cancel_if_not_running(self, *, run_id: UUID) -> AgentRun:
        """Cancel only a Run with no active Worker lease.

        A running graph cannot be cancelled by changing a database bit: it must
        acknowledge the request at a checkpoint-safe boundary.  That worker
        handshake is intentionally a separate control operation.
        """
        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == run_id).with_for_update()
        )
        if row is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if row.status in {
            AgentRunStatus.SUCCEEDED.value,
            AgentRunStatus.FAILED.value,
            AgentRunStatus.CANCELLED.value,
        }:
            return self._from_row(row)
        if row.status == AgentRunStatus.RUNNING.value:
            raise AgentRunConflictError("running AgentRun requires worker safe-point cancellation")
        now = utc_now()
        row.cancel_requested = True
        row.status = AgentRunStatus.CANCELLED.value
        row.updated_at = now
        row.completed_at = now
        await self._session.flush()
        await self._append_lifecycle_event(row, AgentRunEventType.CANCELLED)
        return self._from_row(row)

    async def request_cancel(self, *, run_id: UUID) -> AgentRun:
        """Persist a cancellation request; running work resolves at a Worker safe point."""
        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == run_id).with_for_update()
        )
        if row is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if row.status in {
            AgentRunStatus.SUCCEEDED.value,
            AgentRunStatus.FAILED.value,
            AgentRunStatus.CANCELLED.value,
        }:
            return self._from_row(row)
        if row.status != AgentRunStatus.RUNNING.value:
            return await self.cancel_if_not_running(run_id=run_id)
        row.cancel_requested = True
        row.updated_at = utc_now()
        await self._session.flush()
        await self._append_lifecycle_event(row, AgentRunEventType.CANCELLATION_REQUESTED)
        return self._from_row(row)

    async def invalidate_stale_project_basis(
        self,
        *,
        project_id: UUID,
        current_project_revision: int,
    ) -> tuple[AgentRun, ...]:
        """Fence active runs pinned to an older canonical Project revision.

        Queued/waiting work is terminal immediately because it has no active
        external call.  Running work receives the normal cancellation request;
        its lease owner must acknowledge it at the next safe point.  The
        executor independently rechecks the canonical revision before result
        admission, closing the race with a concurrent project change.
        """

        rows = list(
            (
                await self._session.scalars(
                    select(AgentRunRow)
                    .where(
                        AgentRunRow.project_id == project_id,
                        AgentRunRow.basis_project_revision < current_project_revision,
                        AgentRunRow.status.in_(
                            (
                                AgentRunStatus.QUEUED.value,
                                AgentRunStatus.RUNNING.value,
                                AgentRunStatus.WAITING.value,
                            )
                        ),
                    )
                    .with_for_update()
                )
            ).all()
        )
        now = utc_now()
        invalidated: list[AgentRun] = []
        events: list[tuple[AgentRunRow, AgentRunEventType]] = []
        for row in rows:
            if row.status == AgentRunStatus.RUNNING.value:
                row.cancel_requested = True
                row.updated_at = now
                events.append((row, AgentRunEventType.CANCELLATION_REQUESTED))
            else:
                row.cancel_requested = True
                row.status = AgentRunStatus.CANCELLED.value
                row.lease_owner = None
                row.lease_token = None
                row.lease_expires_at = None
                row.updated_at = now
                row.completed_at = now
                events.append((row, AgentRunEventType.CANCELLED))
            invalidated.append(self._from_row(row))
        await self._session.flush()
        for row, event_type in events:
            await self._append_lifecycle_event(row, event_type, reason="project_basis_invalidated")
        return tuple(invalidated)

    async def acknowledge_cancel_at_safe_point(self, *, claim: AgentRunClaim) -> AgentRun:
        """Only the current lease owner may turn a running cancellation request terminal."""
        row = await self._locked_active_row(claim)
        if not row.cancel_requested:
            raise AgentRunConflictError("AgentRun has no pending cancellation request")
        now = utc_now()
        row.status = AgentRunStatus.CANCELLED.value
        row.lease_owner = None
        row.lease_token = None
        row.lease_expires_at = None
        row.updated_at = now
        row.completed_at = now
        await self._session.flush()
        await self._append_lifecycle_event(row, AgentRunEventType.CANCELLED)
        return self._from_row(row)

    async def _append_lifecycle_event(
        self,
        row: AgentRunRow,
        event_type: AgentRunEventType,
        *,
        reason: str | None = None,
    ) -> int | None:
        """Append a versioned transition only after the queued stream exists.

        Some low-level fixtures and legacy call sites still construct control
        rows directly. They retain their current relation-only behavior until
        their creation path is moved through the authorized queued-event seam.
        This avoids synthesizing a missing initial state while keeping every
        event-backed Run transition in the same database transaction.
        """

        if row.lifecycle_event_version == 0:
            return None
        row.lifecycle_event_version += 1
        row.updated_at = utc_now()
        await self._session.flush()
        run = self._from_row(row)
        payload: dict[str, object] = {"run": run.model_dump(mode="json")}
        if reason is not None:
            payload["reason"] = reason
        artifact_refs = tuple(
            ref
            for ref in (run.run_contract_ref, run.coverage_contract_ref)
            if ref is not None
        )
        metadata = agent_run_lifecycle_metadata(
            run=run,
            aggregate_version=row.lifecycle_event_version,
            payload=payload,
            artifact_refs=artifact_refs,
        )
        # Kept as a local import so the control repository stays usable by
        # low-level tests without introducing an infrastructure import cycle.
        from aidison.infrastructure.store import PostgresDomainStore

        return await PostgresDomainStore(self._session).append_event(
            row.project_id,
            event_type.value,
            payload,
            metadata,
        )

    async def _locked_active_row(self, claim: AgentRunClaim) -> AgentRunRow:
        row = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == claim.run_id).with_for_update()
        )
        if row is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if (
            row.status != AgentRunStatus.RUNNING.value
            or row.current_generation != claim.generation
            or row.lease_token != claim.lease_token
            or row.lease_expires_at is None
            or row.lease_expires_at <= utc_now()
        ):
            raise AgentRunConflictError("stale AgentRun claim cannot mutate run control state")
        return row

    @staticmethod
    def _same_create_request(existing: AgentRun, requested: AgentRun) -> bool:
        """Ignore generated identity/timestamps when replaying one create command."""

        return (
            existing.project_id == requested.project_id
            and existing.kind == requested.kind
            and existing.idempotency_key == requested.idempotency_key
            and existing.basis_hash == requested.basis_hash
            and existing.basis_project_revision == requested.basis_project_revision
            and existing.runtime_binding == requested.runtime_binding
            and existing.thread_id == requested.thread_id
        )

    @staticmethod
    def _from_row(row: AgentRunRow) -> AgentRun:
        return AgentRun.model_validate(
            {
                "id": row.id,
                "project_id": row.project_id,
                "kind": row.kind,
                "idempotency_key": row.idempotency_key,
                "basis_hash": row.basis_hash,
                "basis_project_revision": row.basis_project_revision,
                "runtime_binding": row.runtime_binding,
                "thread_id": row.thread_id,
                "run_contract_ref": row.run_contract_ref,
                "coverage_contract_ref": row.coverage_contract_ref,
                "status": row.status,
                "cancel_requested": row.cancel_requested,
                "current_generation": row.current_generation,
                "admitted_checkpoint": row.admitted_checkpoint,
                "created_at": row.created_at,
                "started_at": row.started_at,
                "updated_at": row.updated_at,
                "completed_at": row.completed_at,
            }
        )
