from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

import aidison.application.execution as execution_module
from aidison.application.execution import DurablePlanExecutor, PlannedDelegation
from aidison.infrastructure.runtime import RuntimeConflictError
from aidison.runtime.contracts import (
    DelegationSpec,
    JobClaim,
    JoinMode,
    JoinPolicy,
    RuntimeWorkItem,
)
from aidison.runtime.planning import OrchestrationPlanRevision, TaskNode


def test_generic_planning_and_execution_do_not_import_research_modules() -> None:
    root = Path(__file__).parents[2]
    generic_modules = (
        root / "src/aidison/runtime/planning.py",
        root / "src/aidison/runtime/contracts.py",
        root / "src/aidison/runtime/__init__.py",
        root / "src/aidison/application/execution.py",
        root / "src/aidison/infrastructure/planning.py",
        root / "src/aidison/infrastructure/runtime.py",
        root / "src/aidison/infrastructure/signals.py",
    )

    forbidden: list[str] = []
    for path in generic_modules:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "research" in node.module:
                    forbidden.append(f"{path.name}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "research" in alias.name:
                        forbidden.append(f"{path.name}:{node.lineno}:{alias.name}")

    assert forbidden == []


def _work() -> RuntimeWorkItem:
    now = datetime.now(UTC)
    return RuntimeWorkItem(
        claim=JobClaim(
            job_id=uuid4(),
            attempt_id=uuid4(),
            attempt_number=1,
            claim_generation=1,
            lease_token=uuid4(),
            lease_owner="unit-test",
            lease_expires_at=now + timedelta(minutes=5),
            basis_hash="a" * 64,
            basis_project_revision=1,
            profile_id="solution-orchestrator",
            profile_revision=1,
        ),
        project_id=uuid4(),
        kind="solution_wave",
    )


def _node(work: RuntimeWorkItem) -> TaskNode:
    return TaskNode(
        logical_key="solution.complete",
        objective="Build one complete solution",
        mode="single",
        role_key="solution-worker",
        profile_id="solution-proposer",
        profile_revision=1,
        budget_ref=f"budget://job/{work.claim.job_id}",
        input_refs=("decision://one",),
        success_criteria=("valid typed solution",),
        stop_criteria=("one proposal",),
        depth=0,
    )


def _spec(work: RuntimeWorkItem, node: TaskNode) -> DelegationSpec:
    return DelegationSpec(
        parent_job_id=work.claim.job_id,
        parent_attempt_id=work.claim.attempt_id,
        parent_claim_generation=work.claim.claim_generation,
        graph_step_id="solution.propose",
        task_kind="solution",
        role_key=node.role_key,
        profile_id=node.profile_id,
        profile_revision=node.profile_revision,
        basis_hash=work.claim.basis_hash,
        shard_key="complete",
        idempotency_key="unit-solution-complete",
        input_refs=node.input_refs,
        token_budget=1_000,
        tool_call_budget=0,
        deadline=work.claim.lease_expires_at,
    )


class _ForbiddenFactory:
    def __call__(self) -> Any:
        raise AssertionError("preflight validation must not open a database session")


@pytest.mark.asyncio
async def test_executor_rejects_empty_and_duplicate_task_waves_before_io() -> None:
    work = _work()
    node = _node(work)
    planned = PlannedDelegation(task_logical_key=node.logical_key, spec=_spec(work, node))
    policy = JoinPolicy(
        mode=JoinMode.ALL_REQUIRED,
        expected_delegation_ids=(planned.spec.delegation_id,),
        min_successes=1,
        deadline=planned.spec.deadline,
    )
    executor = DurablePlanExecutor(
        session_factory=cast(Any, _ForbiddenFactory()),
        join_waiter=cast(Any, object()),
    )

    with pytest.raises(RuntimeConflictError, match="cannot be empty"):
        await executor.dispatch_ready_wave(
            work=work,
            delegations=(),
            policy=policy,
        )
    with pytest.raises(RuntimeConflictError, match="must be unique"):
        await executor.dispatch_ready_wave(
            work=work,
            delegations=(planned, planned),
            policy=policy,
        )


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class _SessionFactory:
    def __call__(self) -> _SessionContext:
        return _SessionContext()


class _PlanStore:
    def __init__(
        self,
        *,
        plan: OrchestrationPlanRevision,
        frontier: tuple[TaskNode, ...],
    ) -> None:
        self.plan = plan
        self.frontier = frontier

    async def create_initial(self, **_: object) -> OrchestrationPlanRevision:
        return self.plan

    async def get_current(self, **_: object) -> OrchestrationPlanRevision:
        return self.plan

    async def list_ready_frontier(self, **_: object) -> tuple[TaskNode, ...]:
        return self.frontier


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_key", "frontier", "mutate_spec", "message"),
    [
        ("solution.missing", True, False, "unknown plan task"),
        ("solution.complete", False, False, "not in the ready frontier"),
        ("solution.complete", True, True, "does not match frozen plan task"),
    ],
)
async def test_executor_fails_closed_before_creating_a_wave(
    monkeypatch: pytest.MonkeyPatch,
    task_key: str,
    frontier: bool,
    mutate_spec: bool,
    message: str,
) -> None:
    work = _work()
    node = _node(work)
    plan = OrchestrationPlanRevision(
        root_job_id=str(work.claim.job_id),
        revision=1,
        basis_hash=work.claim.basis_hash,
        reason="unit plan",
        planner_profile_id=work.claim.profile_id,
        planner_profile_revision=work.claim.profile_revision,
        nodes=(node,),
    )
    store = _PlanStore(plan=plan, frontier=(node,) if frontier else ())
    monkeypatch.setattr(execution_module, "PostgresPlanStore", lambda _: store)
    spec = _spec(work, node)
    if mutate_spec:
        spec = spec.model_copy(update={"role_key": "wrong-worker"})
    planned = PlannedDelegation(task_logical_key=task_key, spec=spec)
    policy = JoinPolicy(
        mode=JoinMode.ALL_REQUIRED,
        expected_delegation_ids=(spec.delegation_id,),
        min_successes=1,
        deadline=spec.deadline,
    )
    executor = DurablePlanExecutor(
        session_factory=cast(Any, _SessionFactory()),
        join_waiter=cast(Any, object()),
    )

    with pytest.raises(RuntimeConflictError, match=message):
        await executor.dispatch_ready_wave(
            work=work,
            delegations=(planned,),
            policy=policy,
            initial_plan=plan,
        )
