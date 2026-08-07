from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.planning import PostgresPlanStore
from aidison.infrastructure.runtime import PostgresRuntime, RuntimeConflictError
from aidison.infrastructure.signals import PostgresSignalBus, SignalUnavailableError
from aidison.runtime.contracts import (
    CommittedJoin,
    DelegationSpec,
    DelegationWave,
    JobClaim,
    JoinPolicy,
    JoinReceipt,
    JoinSnapshot,
    RuntimeWorkItem,
)
from aidison.runtime.planning import OrchestrationPlanRevision, TaskStatus


@dataclass(frozen=True)
class PlannedDelegation:
    """Bind one durable plan node to one frozen delegation specification."""

    task_logical_key: str
    spec: DelegationSpec


class DurableJoinWaiter:
    """Wait for a durable Join, using notifications only as a latency optimization."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        signal_bus: PostgresSignalBus,
        durable_recheck_seconds: float = 30,
        signal_failure_recheck_seconds: float = 0.5,
    ) -> None:
        if durable_recheck_seconds <= 0:
            raise ValueError("durable_recheck_seconds must be positive")
        if signal_failure_recheck_seconds <= 0:
            raise ValueError("signal_failure_recheck_seconds must be positive")
        self._factory = session_factory
        self._signal_bus = signal_bus
        self._durable_recheck_seconds = durable_recheck_seconds
        self._signal_failure_recheck_seconds = signal_failure_recheck_seconds

    async def wait(
        self,
        *,
        project_id: UUID,
        join_group_id: UUID,
        parent_claim: JobClaim,
    ) -> JoinSnapshot:
        while True:
            try:
                async with self._signal_bus.subscribe(project_id=project_id) as subscription:
                    while True:
                        snapshot = await self._inspect(
                            join_group_id=join_group_id,
                            parent_claim=parent_claim,
                        )
                        if snapshot.ready or snapshot.impossible:
                            return snapshot
                        await subscription.wait(
                            timeout_seconds=self._durable_recheck_seconds,
                        )
            except SignalUnavailableError:
                snapshot = await self._inspect(
                    join_group_id=join_group_id,
                    parent_claim=parent_claim,
                )
                if snapshot.ready or snapshot.impossible:
                    return snapshot
                await asyncio.sleep(self._signal_failure_recheck_seconds)

    async def _inspect(
        self,
        *,
        join_group_id: UUID,
        parent_claim: JobClaim,
    ) -> JoinSnapshot:
        async with self._factory() as session:
            return await PostgresRuntime(session).inspect_join(
                join_group_id=join_group_id,
                parent_claim=parent_claim,
            )


class DurablePlanExecutor:
    """Business-neutral parent sequencer over PostgreSQL's durable runtime facts.

    It owns no leases, results, budgets, business merge, or canonical writes. Those
    remain respectively in PostgresRuntime and the calling workflow adapter.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        join_waiter: DurableJoinWaiter,
    ) -> None:
        self._factory = session_factory
        self._join_waiter = join_waiter

    async def find_committed_join(
        self,
        *,
        work: RuntimeWorkItem,
        graph_step_id: str,
    ) -> CommittedJoin | None:
        async with self._factory() as session:
            return await PostgresRuntime(session).find_committed_join(
                parent_claim=work.claim,
                graph_step_id=graph_step_id,
            )

    async def dispatch_ready_wave(
        self,
        *,
        work: RuntimeWorkItem,
        delegations: tuple[PlannedDelegation, ...],
        policy: JoinPolicy,
        initial_plan: OrchestrationPlanRevision | None = None,
    ) -> DelegationWave:
        if not delegations:
            raise RuntimeConflictError("planned delegation wave cannot be empty")
        if len({item.task_logical_key for item in delegations}) != len(delegations):
            raise RuntimeConflictError("planned delegation task keys must be unique")

        async with self._factory() as session:
            store = PostgresPlanStore(session)
            if initial_plan is not None:
                await store.create_initial(
                    claim=work.claim,
                    plan=initial_plan,
                    commit=False,
                )
            current = await store.get_current(root_job_id=work.claim.job_id)
            frontier = await store.list_ready_frontier(root_job_id=work.claim.job_id)
            nodes = {node.logical_key: node for node in current.nodes}
            ready_keys = {node.logical_key for node in frontier}

            for item in delegations:
                node = nodes.get(item.task_logical_key)
                if node is None:
                    raise RuntimeConflictError(
                        f"delegation references unknown plan task: {item.task_logical_key}"
                    )
                if node.status in {TaskStatus.PLANNED, TaskStatus.READY}:
                    if node.logical_key not in ready_keys:
                        raise RuntimeConflictError(
                            f"plan task is not in the ready frontier: {node.logical_key}"
                        )
                elif node.status not in {
                    TaskStatus.DISPATCHED,
                    TaskStatus.RUNNING,
                    TaskStatus.SUCCEEDED,
                }:
                    raise RuntimeConflictError(
                        f"plan task cannot be dispatched from status {node.status}: "
                        f"{node.logical_key}"
                    )
                spec = item.spec
                if (
                    spec.parent_job_id != work.claim.job_id
                    or spec.parent_attempt_id != work.claim.attempt_id
                    or spec.parent_claim_generation != work.claim.claim_generation
                    or spec.basis_hash != work.claim.basis_hash
                    or spec.role_key != node.role_key
                    or spec.profile_id != node.profile_id
                    or spec.profile_revision != node.profile_revision
                    or spec.input_refs != node.input_refs
                ):
                    raise RuntimeConflictError(
                        f"delegation does not match frozen plan task: {node.logical_key}"
                    )

            runtime = PostgresRuntime(session)
            wave = await runtime.create_delegation_wave(
                specs=tuple(item.spec for item in delegations),
                policy=policy,
                commit=False,
            )
            for item, child_job_id in zip(delegations, wave.child_job_ids, strict=True):
                await store.bind_task_job(
                    root_job_id=work.claim.job_id,
                    logical_key=item.task_logical_key,
                    dispatched_job_id=child_job_id,
                    commit=False,
                )
            await session.commit()
            return wave

    async def wait_for_wave(
        self,
        *,
        work: RuntimeWorkItem,
        join_group_id: UUID,
        impossible_message: str,
    ) -> JoinSnapshot:
        snapshot = await self._join_waiter.wait(
            project_id=work.project_id,
            join_group_id=join_group_id,
            parent_claim=work.claim,
        )
        if snapshot.impossible:
            raise RuntimeConflictError(impossible_message)
        return snapshot

    async def commit_join(
        self,
        *,
        join_group_id: UUID,
        merged_proposal_ref: str,
    ) -> JoinReceipt:
        async with self._factory() as session:
            receipt = await PostgresRuntime(session).commit_join(
                join_group_id=join_group_id,
                merged_proposal_ref=merged_proposal_ref,
            )
        if receipt is None:
            raise RuntimeConflictError("durable plan join was not committed")
        return receipt
