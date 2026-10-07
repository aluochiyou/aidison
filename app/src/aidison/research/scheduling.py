"""Dependency-first ready-set projection for admitted Research task results."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.research.langgraph_contracts import TaskEnvelope


class ReadySetInput(BaseModel):
    """Scheduler input derived from immutable tasks and Control admission facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[TaskEnvelope, ...] = Field(min_length=1, max_length=128)
    admitted_result_task_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    in_flight_task_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    available_capacity: int = Field(ge=0, le=16)

    @model_validator(mode="after")
    def task_graph_is_closed_and_acyclic(self) -> ReadySetInput:
        task_ids = tuple(item.id for item in self.tasks)
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("ReadySetInput task ids must be unique")
        if len({task.run_id for task in self.tasks}) != 1:
            raise ValueError("ReadySetInput tasks must belong to the same AgentRun")
        if len({task.basis_hash for task in self.tasks}) != 1:
            raise ValueError("ReadySetInput tasks must share one frozen basis")
        if len({task.plan_revision for task in self.tasks}) != 1:
            raise ValueError("ReadySetInput tasks must share one plan revision")
        known = set(task_ids)
        for task in self.tasks:
            unknown = set(task.dependency_task_ids) - known
            if unknown:
                raise ValueError("task dependency references an unknown task")
        for observed_ids, label in (
            (self.admitted_result_task_ids, "admitted"),
            (self.in_flight_task_ids, "in-flight"),
        ):
            if len(set(observed_ids)) != len(observed_ids):
                raise ValueError(f"ReadySetInput {label} task ids must be unique")
            if set(observed_ids) - known:
                raise ValueError(f"ReadySetInput {label} task ids reference an unknown task")
        if set(self.admitted_result_task_ids) & set(self.in_flight_task_ids):
            raise ValueError("a task cannot be both admitted and in-flight")

        dependencies = {task.id: set(task.dependency_task_ids) for task in self.tasks}
        visiting: set[UUID] = set()
        visited: set[UUID] = set()

        def visit(task_id: UUID) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                raise ValueError("ReadySetInput task graph must be acyclic")
            visiting.add(task_id)
            for dependency_id in dependencies[task_id]:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in dependencies:
            visit(task_id)
        return self


class AdmittedReadySet(BaseModel):
    """A derived scheduling projection; it does not own task completion state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_ids: tuple[UUID, ...]
    deferred_task_ids: tuple[UUID, ...]
    blocked_task_ids: tuple[UUID, ...]


def derive_admitted_ready_set(value: ReadySetInput) -> AdmittedReadySet:
    """Expose only tasks whose dependencies have accepted, not merely returned, results."""

    admitted = set(value.admitted_result_task_ids)
    in_flight = set(value.in_flight_task_ids)
    candidates = sorted(value.tasks, key=lambda task: (task.task_key, str(task.id)))
    eligible = [
        task
        for task in candidates
        if task.id not in admitted
        and task.id not in in_flight
        and set(task.dependency_task_ids).issubset(admitted)
    ]
    blocked = [
        task
        for task in candidates
        if task.id not in admitted
        and task.id not in in_flight
        and not set(task.dependency_task_ids).issubset(admitted)
    ]
    return AdmittedReadySet(
        task_ids=tuple(task.id for task in eligible[: value.available_capacity]),
        deferred_task_ids=tuple(task.id for task in eligible[value.available_capacity :]),
        blocked_task_ids=tuple(task.id for task in blocked),
    )


__all__ = ["AdmittedReadySet", "ReadySetInput", "derive_admitted_ready_set"]
