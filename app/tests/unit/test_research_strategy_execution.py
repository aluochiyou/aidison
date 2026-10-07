from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from aidison.application.agent_run_application import DEFAULT_RESEARCH_RUNTIME_BINDING
from aidison.application.research_run_execution import (
    ResearchRunExecutor,
    _model_failure_code,
    _research_failure_summary,
)
from aidison.domain.models import Module, ResearchStrategyProposal, ResearchStrategyTask
from aidison.providers.model_gateway import ModelInvocationResult, ProviderFailureClass
from aidison.research.coverage import CoverageCompileInput, compile_coverage_contract
from aidison.research.langgraph_contracts import TaskEnvelope
from aidison.research.researcher import ResearchModelInvocationError
from aidison.research.strategy import ResearchRunContract, compile_research_execution_policy
from aidison.runtime.agent_runs import AgentRun, AgentRunKind


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def test_hung_model_timeout_has_a_distinct_user_visible_failure_code() -> None:
    error = ResearchModelInvocationError(
        ModelInvocationResult(
            status="failed",
            failure=ProviderFailureClass.TIMEOUT_AFTER_DISPATCH,
            attempts=(),
        )
    )

    assert _model_failure_code(error) == "research_model_timeout"


def test_repeated_unresolved_gap_has_a_user_steering_failure_summary() -> None:
    summary = _research_failure_summary(
        "repeated_unresolved_gap_requires_user_steering", None
    )

    assert "调整研究范围、来源策略或项目约束" in summary


def test_run_duration_is_measured_from_the_first_worker_claim_not_queue_time() -> None:
    basis_hash = _hash("run-duration")
    first_claim_at = datetime(2026, 9, 12, 10, tzinfo=UTC)
    run = AgentRun(
        project_id=uuid4(),
        kind=AgentRunKind.RESEARCH,
        idempotency_key="run-duration",
        basis_hash=basis_hash,
        basis_project_revision=1,
        runtime_binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
        thread_id="agent-run:duration-test",
        created_at=first_claim_at - timedelta(hours=3),
        started_at=first_claim_at,
    )
    contract = ResearchRunContract(
        agent_run_id=run.id,
        execution_plan_id=uuid4(),
        execution_plan_scope_hash=_hash("scope"),
        basis_hash=basis_hash,
        scope_module_ids=(uuid4(),),
        max_concurrency=1,
        max_token_budget=200_000_000,
        max_duration_seconds=36_000,
    )

    remaining = ResearchRunExecutor._remaining_run_duration_seconds(
        run=run,
        run_contract=contract,
        now=first_claim_at + timedelta(hours=9),
    )

    assert remaining == 3_600


def _module(*, key: str) -> Module:
    return Module(
        project_id=uuid4(),
        requirement_revision_id=uuid4(),
        key=key,
        name=f"{key} module",
        responsibility=f"Own {key} constraints.",
    )


def _task(
    *,
    task_key: str,
    module_id: UUID,
    dependencies: tuple[str, ...] = (),
    research_lenses: tuple[str, ...] = (),
) -> ResearchStrategyTask:
    return ResearchStrategyTask(
        task_key=task_key,
        title=f"Research {task_key}",
        objective=f"Find evidence for {task_key}.",
        module_ids=(module_id,),
        depends_on_task_keys=dependencies,
        priority="must",
        expected_outputs=("evidence", "compatibility"),
        stop_conditions=("A source-backed compatibility conclusion is available.",),
        research_lenses=research_lenses,
    )


def test_strategy_tasks_compile_to_server_owned_coverage_and_dependency_envelopes() -> None:
    power = _module(key="power")
    control = _module(key="control")
    strategy = ResearchStrategyProposal(
        summary="Research power first, then verify control compatibility.",
        scope_module_ids=(power.id, control.id),
        tasks=(
            _task(
                task_key="power.sources",
                module_id=power.id,
                research_lenses=(
                    "official electrical specifications and operating limits",
                    "failure modes and integration trade-offs",
                ),
            ),
            _task(
                task_key="control.compatibility",
                module_id=control.id,
                dependencies=("power.sources",),
            ),
        ),
        source_strategy="primary",
    )
    basis_hash = _hash("strategy-basis")
    coverage = compile_coverage_contract(
        CoverageCompileInput(
            basis_hash=basis_hash,
            objective="Build a safe drone.",
            modules=(power, control),
            strategy_tasks=strategy.tasks,
        )
    )
    run = AgentRun(
        project_id=power.project_id,
        kind=AgentRunKind.RESEARCH,
        basis_hash=basis_hash,
        basis_project_revision=1,
        runtime_binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
        thread_id="agent-run:strategy-test",
        idempotency_key="strategy-test",
        run_contract_ref=(
            "artifact+sha256://" + _hash("run") + "/00000000-0000-0000-0000-000000000001"
        ),
        coverage_contract_ref=(
            "artifact+sha256://"
            + _hash("coverage")
            + "/00000000-0000-0000-0000-000000000002"
        ),
    )

    tasks, questions = ResearchRunExecutor._build_module_tasks(
        run=run,
        coverage=coverage,
        modules=(power, control),
        strategy=strategy,
        collection_policy=compile_research_execution_policy(
            research_depth="deep"
        ).collection,
    )

    by_key = {item.task_key: item for item in tasks}
    power_task = by_key["research.strategy.power.sources"]
    control_task = by_key["research.strategy.control.compatibility"]
    assert "strategy.power.sources" in power_task.coverage_keys
    assert "strategy.control.compatibility" in control_task.coverage_keys
    assert "project.objective" in power_task.coverage_keys
    assert control_task.dependency_task_ids == (power_task.id,)
    assert control_task.collection_policy is not None
    assert control_task.collection_policy.profile == "deep"
    assert control_task.collection_policy.search_depth == "advanced"
    assert "Task objective: Find evidence for control.compatibility." in questions[control_task.id]
    assert "Source strategy: primary" in questions[power_task.id]
    assert (
        "Research lenses: official electrical specifications and operating limits "
        "| failure modes and integration trade-offs"
    ) in questions[power_task.id]
    assert "power.sources" not in questions[control_task.id]
    assert "Method evidence.source_triage:" in questions[power_task.id]
    assert "Method compatibility.interface_check:" in questions[control_task.id]


def test_run_contract_rejects_strategy_scope_that_differs_from_frozen_scope() -> None:
    first_module = uuid4()
    second_module = uuid4()
    strategy = ResearchStrategyProposal(
        summary="Only one module is in scope.",
        scope_module_ids=(first_module,),
        tasks=(_task(task_key="first.research", module_id=first_module),),
        source_strategy="official",
    )

    with pytest.raises(ValueError, match="scope"):
        ResearchRunContract(
            agent_run_id=uuid4(),
            execution_plan_id=uuid4(),
            execution_plan_scope_hash=_hash("scope"),
            basis_hash=_hash("basis"),
            scope_module_ids=(first_module, second_module),
            max_concurrency=2,
            max_token_budget=10_000,
            research_strategy=strategy,
        )


def test_initial_task_token_caps_are_deterministic_and_do_not_favor_completion_order() -> None:
    basis_hash = _hash("fair-budget")
    run_id = uuid4()
    task_a = TaskEnvelope(
        run_id=run_id,
        task_key="research.a",
        basis_hash=basis_hash,
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=(),
        coverage_keys=("a",),
        allowed_tool_ids=(),
        budget_ref=f"budget://agent-run/{run_id}",
        idempotency_key="a",
    )
    task_b = task_a.model_copy(
        update={"id": uuid4(), "task_key": "research.b", "idempotency_key": "b"}
    )
    task_c = task_a.model_copy(
        update={"id": uuid4(), "task_key": "research.c", "idempotency_key": "c"}
    )

    caps = ResearchRunExecutor._allocate_initial_task_token_caps(
        tasks=(task_c, task_a, task_b),
        max_token_budget=10,
    )

    assert caps == {task_a.id: 4, task_b.id: 3, task_c.id: 3}


@pytest.mark.parametrize(
    ("research_depth", "queries", "documents", "search_depth", "patches", "sources"),
    (
        ("focused", 2, 2, "basic", 1, 1),
        ("standard", 3, 3, "basic", 2, 1),
        ("deep", None, None, "advanced", None, 2),
    ),
)
def test_server_compiles_each_approved_research_depth_to_frozen_policy(
    research_depth: str,
    queries: int | None,
    documents: int | None,
    search_depth: str,
    patches: int | None,
    sources: int,
) -> None:
    policy = compile_research_execution_policy(research_depth=research_depth)

    assert policy.collection.max_queries == queries
    assert policy.collection.max_documents_total == documents
    assert policy.collection.search_depth == search_depth
    assert policy.adaptive.max_patch_revisions == patches
    assert policy.minimum_evidence_sources_for_must == sources
    assert policy.minimum_evidence_origins_for_must == sources
