from __future__ import annotations

from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from aidison.research.langgraph_contracts import TaskEnvelope
from aidison.research.scheduling import ReadySetInput, derive_admitted_ready_set


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _task(
    *,
    task_key: str,
    task_id: UUID | None = None,
    run_id: UUID | None = None,
    dependency_task_ids: tuple[UUID, ...] = (),
) -> TaskEnvelope:
    return TaskEnvelope(
        id=task_id or uuid4(),
        run_id=run_id or uuid4(),
        task_key=task_key,
        basis_hash=_hash("basis"),
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=dependency_task_ids,
        coverage_keys=(f"coverage.{task_key}",),
        allowed_tool_ids=(),
        budget_ref="budget://run/1",
        idempotency_key=f"task-{task_key}",
    )


def test_ready_set_only_unlocks_dependents_after_dependency_result_is_admitted() -> None:
    run_id = uuid4()
    first = _task(task_key="first", run_id=run_id)
    second = _task(task_key="second", run_id=run_id, dependency_task_ids=(first.id,))
    ready_before = derive_admitted_ready_set(
        ReadySetInput(tasks=(second, first), admitted_result_task_ids=(), available_capacity=4)
    )
    ready_after = derive_admitted_ready_set(
        ReadySetInput(
            tasks=(second, first),
            admitted_result_task_ids=(first.id,),
            available_capacity=4,
        )
    )

    assert ready_before.task_ids == (first.id,)
    assert ready_after.task_ids == (second.id,)


def test_ready_set_is_deterministic_and_respects_capacity_without_marking_work_complete() -> None:
    run_id = uuid4()
    alpha = _task(task_key="alpha", run_id=run_id)
    beta = _task(task_key="beta", run_id=run_id)
    ready = derive_admitted_ready_set(
        ReadySetInput(tasks=(beta, alpha), admitted_result_task_ids=(), available_capacity=1)
    )

    assert ready.task_ids == (alpha.id,)
    assert ready.deferred_task_ids == (beta.id,)


def test_ready_set_rejects_unknown_or_cyclic_dependencies() -> None:
    missing = uuid4()
    orphan = _task(task_key="orphan", dependency_task_ids=(missing,))
    with pytest.raises(ValueError, match="unknown task"):
        ReadySetInput(tasks=(orphan,), admitted_result_task_ids=(), available_capacity=1)

    run_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    first = _task(
        task_key="first", task_id=first_id, run_id=run_id, dependency_task_ids=(second_id,)
    )
    second = _task(
        task_key="second", task_id=second_id, run_id=run_id, dependency_task_ids=(first_id,)
    )
    with pytest.raises(ValueError, match="acyclic"):
        ReadySetInput(tasks=(first, second), admitted_result_task_ids=(), available_capacity=1)


def test_ready_set_rejects_tasks_from_different_runs() -> None:
    with pytest.raises(ValueError, match="same AgentRun"):
        ReadySetInput(
            tasks=(_task(task_key="first"), _task(task_key="second")),
            admitted_result_task_ids=(),
            available_capacity=1,
        )
