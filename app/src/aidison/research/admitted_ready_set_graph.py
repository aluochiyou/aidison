"""LangGraph completion-driven executor driven by durable Result Admission facts."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.func import task
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typing_extensions import TypedDict

from aidison.application.admitted_ready_set import AdmittedReadySetApplication
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.research.langgraph_contracts import ProposalManifest, TaskEnvelope


class ReadyTaskExecutor(Protocol):
    """One capability invocation; it must durably record its Result/Admission."""

    async def execute(self, *, task: TaskEnvelope) -> None: ...


class ReadySetFinalizer(Protocol):
    """Derive one reviewable proposal only from already admitted task results."""

    async def finalize(
        self,
        *,
        tasks: tuple[TaskEnvelope, ...],
        admitted_task_ids: tuple[UUID, ...],
    ) -> ProposalManifest: ...


class AdmittedReadySetGraphState(TypedDict, total=False):
    """Checkpointed scheduling projection; durable result truth remains in Control."""

    # Checkpoint state must remain JSON primitives.  Rebuild Pydantic contracts
    # inside nodes instead of persisting unregistered Python/asyncpg UUID types.
    tasks: tuple[dict[str, Any], ...]
    available_capacity: int
    attempted_task_ids: tuple[str, ...]
    ready_task_ids: tuple[str, ...]
    deferred_task_ids: tuple[str, ...]
    blocked_task_ids: tuple[str, ...]
    admitted_task_ids: tuple[str, ...]
    wave_count: int
    max_waves: int | None
    terminal_outcome: str
    proposal_manifest_ref: str
    proposal_manifest_hash: str


def _tasks(state: AdmittedReadySetGraphState) -> tuple[TaskEnvelope, ...]:
    return tuple(TaskEnvelope.model_validate(item) for item in state["tasks"])


def build_admitted_ready_set_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any],
    session_factory: async_sessionmaker[AsyncSession],
    executor: ReadyTaskExecutor,
    finalizer: ReadySetFinalizer | None = None,
) -> Any:
    """Build a bounded completion-driven task driver without a second runtime.

    Each leaf invocation is a LangGraph ``@task``. As each leaf completes, the
    driver reads PostgreSQL admissions again before it fills the free capacity.
    A producer return alone therefore never unlocks a dependency, while an
    admitted result can unlock its dependent task before unrelated peers end.
    """

    def execute_one(task_envelope: TaskEnvelope) -> Any:
        """Schedule one business-stable leaf task.

        Functional API task persistence includes the parent call position.  The
        task name therefore must also contain the immutable TaskEnvelope id:
        on a recovery pass, already admitted tasks can be omitted without a
        later task consuming their saved pending write.
        """

        async def execute_assigned_task() -> str:
            await executor.execute(task=task_envelope)
            return str(task_envelope.id)

        # LangGraph's ParamSpec overload cannot infer a no-argument closure
        # when the task name is runtime-derived; its runtime contract is still
        # exactly ``Callable[[], Awaitable[UUID]]``.
        task_function: Any = task(name=f"aidison_execute_task_{task_envelope.id}")(
            execute_assigned_task  # type: ignore[arg-type]
        )
        return task_function()

    async def select_ready(
        state: AdmittedReadySetGraphState,
    ) -> AdmittedReadySetGraphState:
        tasks = _tasks(state)
        attempted = tuple(UUID(item) for item in state.get("attempted_task_ids", ()))
        max_waves = state.get("max_waves")
        wave_count = state.get("wave_count", 0)
        if max_waves is not None and wave_count >= max_waves:
            return {"terminal_outcome": "wave_limit"}
        async with session_factory() as session:
            projection = await AdmittedReadySetApplication(session).derive(
                tasks=tasks,
                available_capacity=state["available_capacity"],
                in_flight_task_ids=attempted,
            )
            # A Run may already contain accepted gap or verifier leaves that
            # belong to another immutable graph revision.  They are durable
            # Control history, but cannot satisfy or complete this graph.
            known_task_ids = {item.id for item in tasks}
            admitted = tuple(
                task_id
                for task_id in await AgentResultStore(session).admitted_task_ids(
                    run_id=tasks[0].run_id
                )
                if task_id in known_task_ids
            )
        if not projection.task_ids:
            outcome = "complete" if len(admitted) == len(tasks) else "partial"
            return {
                "ready_task_ids": (),
                "deferred_task_ids": tuple(str(item) for item in projection.deferred_task_ids),
                "blocked_task_ids": tuple(str(item) for item in projection.blocked_task_ids),
                "admitted_task_ids": tuple(str(item) for item in admitted),
                "terminal_outcome": outcome,
            }
        return {
            "ready_task_ids": tuple(str(item) for item in projection.task_ids),
            "deferred_task_ids": tuple(str(item) for item in projection.deferred_task_ids),
            "blocked_task_ids": tuple(str(item) for item in projection.blocked_task_ids),
            "admitted_task_ids": tuple(str(item) for item in admitted),
        }

    async def execute_ready(
        state: AdmittedReadySetGraphState,
    ) -> AdmittedReadySetGraphState:
        by_id = {item.id: item for item in _tasks(state)}
        settled = {UUID(item) for item in state.get("attempted_task_ids", ())}
        active: dict[Any, UUID] = {}

        async def fill_available_capacity() -> None:
            available_capacity = state["available_capacity"] - len(active)
            if available_capacity <= 0:
                return
            async with session_factory() as session:
                projection = await AdmittedReadySetApplication(session).derive(
                    tasks=_tasks(state),
                    available_capacity=available_capacity,
                    in_flight_task_ids=tuple(
                        sorted(settled | set(active.values()), key=str)
                    ),
                )
            for task_id in projection.task_ids:
                active[execute_one(by_id[task_id])] = task_id

        await fill_available_capacity()
        while active:
            done, _ = await asyncio.wait(
                active,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for future in done:
                task_id = active.pop(future)
                completed_task_id = await future
                if completed_task_id != str(task_id):
                    raise RuntimeError("LangGraph task returned an unexpected task id")
                settled.add(task_id)
            await fill_available_capacity()
        return {
            "attempted_task_ids": tuple(str(item) for item in sorted(settled, key=str)),
            "wave_count": state.get("wave_count", 0) + 1,
        }

    async def finalize(
        state: AdmittedReadySetGraphState,
    ) -> AdmittedReadySetGraphState:
        if finalizer is None:  # pragma: no cover - guarded by graph routing
            raise RuntimeError("ready-set graph has no proposal finalizer")
        proposal = await finalizer.finalize(
            tasks=_tasks(state),
            admitted_task_ids=tuple(UUID(item) for item in state["admitted_task_ids"]),
        )
        return {
            "proposal_manifest_ref": proposal.artifact_ref,
            "proposal_manifest_hash": proposal.manifest_hash,
        }

    def await_user_decision(state: AdmittedReadySetGraphState) -> AdmittedReadySetGraphState:
        interrupt(
            {
                "kind": "research_proposal_review",
                "proposal_manifest_ref": state["proposal_manifest_ref"],
                "proposal_manifest_hash": state["proposal_manifest_hash"],
            }
        )
        return {}

    def route_after_selection(state: AdmittedReadySetGraphState) -> str:
        if state.get("ready_task_ids"):
            return "execute_ready"
        if finalizer is not None and state.get("terminal_outcome") == "complete":
            return "finalize"
        return END

    builder = StateGraph(AdmittedReadySetGraphState)
    builder.add_node("select_ready", cast(Any, select_ready))
    builder.add_node("execute_ready", cast(Any, execute_ready))
    if finalizer is not None:
        builder.add_node("finalize", cast(Any, finalize))
        builder.add_node("await_user_decision", cast(Any, await_user_decision))
    builder.add_edge(START, "select_ready")
    builder.add_conditional_edges("select_ready", route_after_selection)
    builder.add_edge("execute_ready", "select_ready")
    if finalizer is not None:
        builder.add_edge("finalize", "await_user_decision")
        builder.add_edge("await_user_decision", END)
    return builder.compile(checkpointer=checkpointer, name="aidison_admitted_ready_set_v1")


__all__ = [
    "AdmittedReadySetGraphState",
    "ReadySetFinalizer",
    "ReadyTaskExecutor",
    "build_admitted_ready_set_graph",
]
