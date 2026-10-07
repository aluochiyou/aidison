"""Application projection from durable admissions to the next runnable Research tasks."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.agent_results import AgentResultStore
from aidison.research.langgraph_contracts import TaskEnvelope
from aidison.research.scheduling import AdmittedReadySet, ReadySetInput, derive_admitted_ready_set


class AdmittedReadySetApplication:
    """Use Control admission facts, never producer completion, to derive readiness."""

    def __init__(self, session: AsyncSession) -> None:
        self._result_store = AgentResultStore(session)

    async def derive(
        self,
        *,
        tasks: tuple[TaskEnvelope, ...],
        available_capacity: int,
        in_flight_task_ids: tuple[UUID, ...] = (),
    ) -> AdmittedReadySet:
        if not tasks:
            raise ValueError("AdmittedReadySetApplication requires at least one task")
        run_ids = {task.run_id for task in tasks}
        if len(run_ids) != 1:
            raise ValueError("AdmittedReadySetApplication tasks must belong to one AgentRun")
        run_id = next(iter(run_ids))
        # A gap/verifier patch may have admitted a task outside this graph
        # revision. It belongs to Control history, but it must not invalidate
        # the current graph's ready-set projection. Only task identities in
        # this immutable graph input can participate in dependency checks.
        known_task_ids = {task.id for task in tasks}
        admitted_task_ids = tuple(
            task_id
            for task_id in await self._result_store.admitted_task_ids(run_id=run_id)
            if task_id in known_task_ids
        )
        return derive_admitted_ready_set(
            ReadySetInput(
                tasks=tasks,
                admitted_result_task_ids=admitted_task_ids,
                in_flight_task_ids=tuple(
                    task_id for task_id in in_flight_task_ids if task_id not in admitted_task_ids
                ),
                available_capacity=available_capacity,
            )
        )


__all__ = ["AdmittedReadySetApplication"]
