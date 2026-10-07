"""Unit coverage for the model-visible, approved strategy planning context."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from aidison.application.research_strategy_planning import (
    ResearchStrategyPlanningError,
    ResearchStrategyPlanningService,
)
from aidison.domain.models import Module, RequirementRevision, RequirementStatus


@pytest.mark.asyncio
async def test_strategy_planning_preserves_safe_schema_diagnostics_after_retry() -> None:
    """The service can diagnose invalid model output without retaining raw text."""

    project_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="power",
        name="动力",
        responsibility="提供稳定动力。",
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作安全无人机。",
    )
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=(
            AIMessage(content="{not valid JSON}"),
            AIMessage(content="{still not valid JSON}"),
        )
    )
    model.bind.return_value = bound

    with pytest.raises(ResearchStrategyPlanningError) as raised:
        await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
            objective="核对动力约束。",
            requirement=requirement,
            scope_modules=(module,),
        )

    assert raised.value.diagnostics == ("schema:$:json_invalid",)


@pytest.mark.asyncio
async def test_hierarchical_deep_strategy_expands_only_complex_modules_and_adds_handoffs() -> None:
    """A small global skeleton is expanded locally and joined by approved edges."""

    project_id = uuid4()
    requirement_id = uuid4()
    power = Module(
        project_id=project_id,
        requirement_revision_id=requirement_id,
        key="power",
        name="动力",
        responsibility="提供稳定供电。",
    )
    control = Module(
        project_id=project_id,
        requirement_revision_id=requirement_id,
        key="control",
        name="控制",
        responsibility="在动力约束下维持安全飞行。",
        dependency_ids=(power.id,),
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作安全无人机。",
    )

    skeleton_model = MagicMock(spec=BaseChatModel)
    skeleton_bound = MagicMock()
    skeleton_bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "先建立动力约束，再核验控制兼容性。",
                    "scope_module_ids": [str(power.id), str(control.id)],
                    "tasks": [
                        {
                            "task_key": "power.seed",
                            "title": "动力约束种子",
                            "objective": "明确供电约束。",
                            "module_ids": [str(power.id)],
                            "depends_on_task_keys": [],
                            "priority": "must",
                            "expected_outputs": ["evidence", "constraint"],
                            "stop_conditions": ["记录约束或缺口。"],
                            "research_lenses": ["规格约束"],
                        },
                        {
                            "task_key": "control.seed",
                            "title": "控制兼容性种子",
                            "objective": "核验控制与动力接口。",
                            "module_ids": [str(control.id)],
                            "depends_on_task_keys": [],
                            "priority": "must",
                            "expected_outputs": ["evidence", "compatibility"],
                            "stop_conditions": ["记录兼容性或缺口。"],
                            "research_lenses": ["接口风险"],
                        },
                    ],
                    "source_strategy": "mixed",
                },
                ensure_ascii=False,
            )
        )
    )
    skeleton_model.bind.return_value = skeleton_bound

    control_model = MagicMock(spec=BaseChatModel)
    control_bound = MagicMock()
    control_bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "先约束接口，再比较控制兼容性。",
                    "scope_module_ids": [str(control.id)],
                    "tasks": [
                        {
                            "task_key": "constraints",
                            "title": "控制接口约束",
                            "objective": "建立控制模块的接口与安全约束。",
                            "module_ids": [str(control.id)],
                            "depends_on_task_keys": [],
                            "priority": "must",
                            "expected_outputs": ["evidence", "constraint"],
                            "stop_conditions": ["记录约束或缺口。"],
                            "research_lenses": ["接口规格"],
                        },
                        {
                            "task_key": "compatibility",
                            "title": "控制兼容性",
                            "objective": "比较控制候选并核验动力接口兼容性。",
                            "module_ids": [str(control.id)],
                            "depends_on_task_keys": ["constraints"],
                            "priority": "must",
                            "expected_outputs": ["evidence", "compatibility"],
                            "stop_conditions": ["记录兼容性或缺口。"],
                            "research_lenses": ["失效隔离"],
                        },
                    ],
                    "source_strategy": "mixed",
                },
                ensure_ascii=False,
            )
        )
    )
    control_model.bind.return_value = control_bound
    models = iter((skeleton_model, control_model))

    strategy = await ResearchStrategyPlanningService(
        model_factory=lambda: next(models)
    ).propose(
        objective="验证动力与控制的接口。",
        requirement=requirement,
        scope_modules=(power, control),
        research_depth="deep",
        planning_mode="hierarchical",
        planning_max_concurrency=2,
    )

    by_key = {task.task_key: task for task in strategy.tasks}
    assert set(by_key) == {
        "power.power.seed",
        "control.constraints",
        "control.compatibility",
    }
    assert by_key["control.constraints"].depends_on_task_keys == (
        "power.power.seed",
    )
    assert by_key["control.compatibility"].depends_on_task_keys == (
        "control.constraints",
    )
    skeleton_context = json.loads(skeleton_bound.ainvoke.await_args.args[0][1]["content"])
    assert skeleton_context["strategy_generation_mode"] == "global_skeleton"
    local_context = json.loads(control_bound.ainvoke.await_args.args[0][1]["content"])
    assert local_context["strategy_generation_mode"] == "module_detail"


@pytest.mark.asyncio
async def test_strategy_planner_receives_all_approved_requirement_context() -> None:
    project_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="power",
        name="动力",
        responsibility="在室内安全地提供动力。",
        acceptance=("连续巡检时噪声必须低于 70 dB", "必须兼容现有 4S 电池"),
        open_questions=("续航与载荷的取舍边界是什么？",),
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作室内巡检无人机。",
        hard_constraints=("噪声必须低于 70 dB",),
        preferences=("优先可维护性",),
        available_resources=("已有 4S 电池",),
        usage_context="在狭窄仓库通道内巡检。",
        budget_context="总硬件预算不超过 CNY 3000。",
        skill_context="团队能焊接和调参，但不做定制 PCB。",
        unknowns=("续航和载荷如何权衡？",),
    )
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "先核对噪声、预算和电池兼容性。",
                    "scope_module_ids": [str(module.id)],
                    "tasks": [
                        {
                            "task_key": "power.tradeoffs",
                            "title": "动力取舍",
                            "objective": "比较满足噪声、预算和现有电池的动力方案。",
                            "module_ids": [str(module.id)],
                            "depends_on_task_keys": [],
                            "priority": "must",
                            "expected_outputs": ["evidence", "candidate", "constraint"],
                            "stop_conditions": ["记录可核验的取舍或明确缺口。"],
                        }
                    ],
                    "deferred_questions": ["续航和载荷如何权衡？"],
                    "source_strategy": "official",
                },
                ensure_ascii=False,
            )
        )
    )
    model.bind.return_value = bound

    strategy = await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
        objective="选择适合仓库巡检的动力方案。",
        requirement=requirement,
        scope_modules=(module,),
        research_depth="standard",
        source_strategy="official",
    )

    planning_context = json.loads(bound.ainvoke.await_args.args[0][1]["content"])
    approved = planning_context["requirement"]
    assert approved["usage_context"] == requirement.usage_context
    assert approved["budget_context"] == requirement.budget_context
    assert approved["skill_context"] == requirement.skill_context
    assert approved["available_resources"] == list(requirement.available_resources)
    assert approved["unknowns"] == list(requirement.unknowns)
    assert planning_context["source_strategy_preference"] == "official"
    [module_scope] = planning_context["allowed_scope"]
    assert module_scope["acceptance"] == list(module.acceptance)
    assert module_scope["open_questions"] == list(module.open_questions)
    assert module_scope["deep_decomposition_required"] is True
    assert strategy.tasks[0].task_key == "power.tradeoffs"


@pytest.mark.asyncio
async def test_strategy_planner_rejects_a_model_that_changes_user_source_preference() -> None:
    project_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="power",
        name="动力",
        responsibility="提供稳定电力。",
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作安全无人机。",
    )
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "研究动力约束。",
                    "scope_module_ids": [str(module.id)],
                    "tasks": [
                        {
                            "task_key": "power.sources",
                            "title": "动力资料",
                            "objective": "收集可核验的动力约束。",
                            "module_ids": [str(module.id)],
                            "depends_on_task_keys": [],
                            "priority": "must",
                            "expected_outputs": ["evidence"],
                            "stop_conditions": ["记录证据或明确缺口。"],
                        }
                    ],
                    "source_strategy": "primary",
                },
                ensure_ascii=False,
            )
        )
    )
    model.bind.return_value = bound

    with pytest.raises(ResearchStrategyPlanningError, match="requested source strategy"):
        await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
            objective="优先收集官方动力规格。",
            requirement=requirement,
            scope_modules=(module,),
            source_strategy="official",
        )


@pytest.mark.asyncio
async def test_strategy_planner_rejects_a_selected_module_without_a_task() -> None:
    project_id = uuid4()
    power = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="power",
        name="动力",
        responsibility="提供动力。",
    )
    control = Module(
        project_id=project_id,
        requirement_revision_id=power.requirement_revision_id,
        key="control",
        name="控制",
        responsibility="控制飞行。",
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作无人机。",
    )
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "只研究动力。",
                    "scope_module_ids": [str(power.id), str(control.id)],
                    "tasks": [
                        {
                            "task_key": "power.only",
                            "title": "动力",
                            "objective": "研究动力。",
                            "module_ids": [str(power.id)],
                            "depends_on_task_keys": [],
                            "priority": "must",
                            "expected_outputs": ["evidence"],
                            "stop_conditions": ["记录结论或缺口。"],
                        }
                    ],
                    "source_strategy": "primary",
                },
                ensure_ascii=False,
            )
        )
    )
    model.bind.return_value = bound

    with pytest.raises(ResearchStrategyPlanningError, match="omits a selected module"):
        await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
            objective="比较动力和控制方案。",
            requirement=requirement,
            scope_modules=(power, control),
        )


@pytest.mark.asyncio
async def test_deep_strategy_gets_one_targeted_repair_for_missing_constraint_lens() -> None:
    project_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="power",
        name="动力",
        responsibility="提供动力。",
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作低噪声无人机。",
        hard_constraints=("噪声必须低于 70 dB",),
    )

    def strategy_payload(*, outputs: list[str]) -> str:
        return json.dumps(
            {
                "summary": "评估动力方案。",
                "scope_module_ids": [str(module.id)],
                "tasks": [
                    {
                        "task_key": "power.assessment",
                        "title": "动力评估",
                        "objective": "比较动力候选与低噪声约束。",
                        "module_ids": [str(module.id)],
                        "depends_on_task_keys": [],
                        "priority": "must",
                        "expected_outputs": outputs,
                        "stop_conditions": ["记录证据充分的候选或明确缺口。"],
                    }
                ],
                "source_strategy": "primary",
            },
            ensure_ascii=False,
        )

    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=(
            AIMessage(content=strategy_payload(outputs=["evidence", "candidate"])),
            AIMessage(
                content=strategy_payload(outputs=["evidence", "candidate", "constraint"])
            ),
        )
    )
    model.bind.return_value = bound

    strategy = await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
        objective="选择低噪声动力方案。",
        requirement=requirement,
        scope_modules=(module,),
        research_depth="deep",
    )

    assert strategy.tasks[0].expected_outputs == ("evidence", "candidate", "constraint")
    assert bound.ainvoke.await_count == 2
    repair_context = json.loads(bound.ainvoke.await_args_list[1].args[0][1]["content"])
    assert repair_context["strategy_review_feedback"] == {
        "issues": ["power:missing_constraint_output"],
        "instruction": (
            "Revise the complete strategy to address every listed issue without changing scope."
        ),
    }


@pytest.mark.asyncio
async def test_deep_strategy_decomposes_a_module_with_a_dependency_boundary() -> None:
    """Deep mode must not compress a dependency-facing module into one model turn."""

    project_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="control",
        name="控制",
        responsibility="在上游动力约束下维持稳定控制。",
        dependency_ids=(uuid4(),),
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作稳定无人机。",
    )

    def payload(*, decomposed: bool) -> str:
        tasks = [
            {
                "task_key": "control.assessment",
                "title": "控制评估",
                "objective": "在上游约束下比较控制候选并核验兼容性。",
                "module_ids": [str(module.id)],
                "depends_on_task_keys": [],
                "priority": "must",
                "expected_outputs": ["evidence", "candidate", "compatibility"],
                "stop_conditions": ["记录可核验的兼容性结论或明确缺口。"],
                "research_lenses": ["接口规格与验收约束", "失效模式与兼容性风险"],
            }
        ]
        if decomposed:
            tasks = [
                {
                    "task_key": "control.constraints",
                    "title": "控制接口约束",
                    "objective": "建立控制模块必须满足的上游接口与安全约束。",
                    "module_ids": [str(module.id)],
                    "depends_on_task_keys": [],
                    "priority": "must",
                    "expected_outputs": ["evidence", "constraint"],
                    "stop_conditions": ["记录来源支持的接口约束或明确缺口。"],
                    "research_lenses": ["接口规格与验收约束"],
                },
                {
                    "task_key": "control.compatibility",
                    "title": "控制兼容性",
                    "objective": "基于已建立的约束比较控制候选并核验兼容性风险。",
                    "module_ids": [str(module.id)],
                    "depends_on_task_keys": ["control.constraints"],
                    "priority": "must",
                    "expected_outputs": ["evidence", "candidate", "compatibility"],
                    "stop_conditions": ["记录可核验的兼容性结论或明确缺口。"],
                    "research_lenses": ["失效模式与兼容性风险"],
                },
            ]
        return json.dumps(
            {
                "summary": "先核对控制接口约束，再评估候选兼容性。",
                "scope_module_ids": [str(module.id)],
                "tasks": tasks,
                "source_strategy": "mixed",
            },
            ensure_ascii=False,
        )

    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=(
            AIMessage(content=payload(decomposed=False)),
            AIMessage(content=payload(decomposed=True)),
        )
    )
    model.bind.return_value = bound

    strategy = await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
        objective="在上游动力约束下选择控制方案。",
        requirement=requirement,
        scope_modules=(module,),
        research_depth="deep",
    )

    assert [task.task_key for task in strategy.tasks] == [
        "control.constraints",
        "control.compatibility",
    ]
    assert strategy.tasks[1].depends_on_task_keys == ("control.constraints",)
    assert bound.ainvoke.await_count == 2
    repair_context = json.loads(bound.ainvoke.await_args_list[1].args[0][1]["content"])
    assert repair_context["strategy_review_feedback"]["issues"] == [
        "control:missing_deep_task_decomposition"
    ]


@pytest.mark.asyncio
async def test_deep_strategy_repairs_missing_module_dependency_handoff() -> None:
    project_id = uuid4()
    requirement_id = uuid4()
    power = Module(
        project_id=project_id,
        requirement_revision_id=requirement_id,
        key="power",
        name="动力",
        responsibility="提供电力约束。",
    )
    control = Module(
        project_id=project_id,
        requirement_revision_id=requirement_id,
        key="control",
        name="控制",
        responsibility="核验控制兼容性。",
        dependency_ids=(power.id,),
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作安全无人机。",
    )

    def strategy_payload(*, control_dependencies: list[str], decomposed: bool = False) -> str:
        control_tasks = [
            {
                "task_key": "control.compatibility",
                "title": "控制兼容性",
                "objective": "核验控制模块与动力约束的兼容性。",
                "module_ids": [str(control.id)],
                "depends_on_task_keys": control_dependencies,
                "priority": "must",
                "expected_outputs": ["evidence", "compatibility"],
                "stop_conditions": ["记录来源兼容性或缺口。"],
                "research_lenses": ["接口规格与验收约束", "失效模式与兼容性风险"],
            }
        ]
        if decomposed:
            control_tasks = [
                {
                    "task_key": "control.constraints",
                    "title": "控制接口约束",
                    "objective": "建立控制模块依赖动力的接口约束。",
                    "module_ids": [str(control.id)],
                    "depends_on_task_keys": control_dependencies,
                    "priority": "must",
                    "expected_outputs": ["evidence", "constraint"],
                    "stop_conditions": ["记录接口约束或缺口。"],
                    "research_lenses": ["接口规格与验收约束"],
                },
                {
                    "task_key": "control.compatibility",
                    "title": "控制兼容性",
                    "objective": "基于接口约束核验控制模块与动力的兼容性。",
                    "module_ids": [str(control.id)],
                    "depends_on_task_keys": ["control.constraints"],
                    "priority": "must",
                    "expected_outputs": ["evidence", "compatibility"],
                    "stop_conditions": ["记录来源兼容性或缺口。"],
                    "research_lenses": ["失效模式与兼容性风险"],
                },
            ]
        return json.dumps(
            {
                "summary": "先确定动力约束，再核验控制兼容性。",
                "scope_module_ids": [str(power.id), str(control.id)],
                "tasks": [
                    {
                        "task_key": "power.sources",
                        "title": "动力约束",
                        "objective": "检索动力约束。",
                        "module_ids": [str(power.id)],
                        "depends_on_task_keys": [],
                        "priority": "must",
                        "expected_outputs": ["evidence", "constraint"],
                        "stop_conditions": ["记录来源约束或缺口。"],
                    },
                    *control_tasks,
                ],
                "source_strategy": "mixed",
            },
            ensure_ascii=False,
        )

    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=(
            AIMessage(content=strategy_payload(control_dependencies=[])),
            AIMessage(
                content=strategy_payload(
                    control_dependencies=["power.sources"], decomposed=True
                )
            ),
        )
    )
    model.bind.return_value = bound

    strategy = await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
        objective="先研究动力，再研究控制兼容性。",
        requirement=requirement,
        scope_modules=(power, control),
        research_depth="deep",
    )

    assert strategy.tasks[1].depends_on_task_keys == ("power.sources",)
    assert strategy.tasks[2].depends_on_task_keys == ("control.constraints",)
    assert bound.ainvoke.await_count == 2
    repair_context = json.loads(bound.ainvoke.await_args_list[1].args[0][1]["content"])
    assert repair_context["strategy_review_feedback"]["issues"] == [
        "control:missing_deep_task_decomposition",
        "control:missing_dependency_handoff",
    ]


@pytest.mark.asyncio
async def test_deep_strategy_repairs_missing_decision_tradeoff_and_unknown_handling() -> None:
    project_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="power",
        name="动力",
        responsibility="提供动力。",
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作低噪声无人机。",
        preferences=("优先可维护性",),
        budget_context="总硬件预算不超过 CNY 3000。",
        unknowns=("续航和载荷如何权衡？",),
    )

    def strategy_payload(
        *,
        outputs: list[str],
        deferred_questions: list[str],
        decision_notes: list[str] | None = None,
    ) -> str:
        return json.dumps(
            {
                "summary": "评估动力方案。",
                "decision_notes": decision_notes or [],
                "scope_module_ids": [str(module.id)],
                "tasks": [
                    {
                        "task_key": "power.assessment",
                        "title": "动力评估",
                        "objective": "评估动力方案。",
                        "module_ids": [str(module.id)],
                        "depends_on_task_keys": [],
                        "priority": "must",
                        "expected_outputs": outputs,
                        "stop_conditions": ["记录结论或明确缺口。"],
                    }
                ],
                "deferred_questions": deferred_questions,
                "source_strategy": "primary",
            },
            ensure_ascii=False,
        )

    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=(
            AIMessage(content=strategy_payload(outputs=["evidence"], deferred_questions=[])),
            AIMessage(
                content=strategy_payload(
                    outputs=["evidence", "candidate"],
                    deferred_questions=["续航和载荷如何权衡？"],
                    decision_notes=["优先维护性可能降低峰值性能，需以预算内候选比较验证。"],
                )
            ),
        )
    )
    model.bind.return_value = bound

    strategy = await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
        objective="选择低噪声动力方案。",
        requirement=requirement,
        scope_modules=(module,),
        research_depth="deep",
    )

    assert strategy.tasks[0].expected_outputs == ("evidence", "candidate")
    assert strategy.deferred_questions == ("续航和载荷如何权衡？",)
    assert strategy.decision_notes == ("优先维护性可能降低峰值性能，需以预算内候选比较验证。",)
    repair_context = json.loads(bound.ainvoke.await_args_list[1].args[0][1]["content"])
    assert repair_context["strategy_review_feedback"]["issues"] == [
        "strategy:missing_candidate_output_for_decision_context",
        "strategy:missing_decision_notes_for_decision_context",
        "strategy:missing_deferred_question_or_risk_note",
    ]


@pytest.mark.asyncio
async def test_deep_strategy_rejects_plan_when_targeted_repair_still_misses_required_lens() -> None:
    project_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="control",
        name="控制",
        responsibility="维持稳定控制。",
        dependency_ids=(uuid4(),),
    )
    requirement = RequirementRevision(
        project_id=project_id,
        revision=1,
        status=RequirementStatus.APPROVED,
        approved_at=datetime.now(UTC),
        goal="制作稳定无人机。",
    )
    response = AIMessage(
        content=json.dumps(
            {
                "summary": "研究控制方案。",
                "scope_module_ids": [str(module.id)],
                "tasks": [
                    {
                        "task_key": "control.assessment",
                        "title": "控制评估",
                        "objective": "比较控制候选。",
                        "module_ids": [str(module.id)],
                        "depends_on_task_keys": [],
                        "priority": "must",
                        "expected_outputs": ["evidence", "candidate"],
                        "stop_conditions": ["记录结论或明确缺口。"],
                    }
                ],
                "source_strategy": "primary",
            },
            ensure_ascii=False,
        )
    )
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=(response, response))
    model.bind.return_value = bound

    with pytest.raises(
        ResearchStrategyPlanningError,
        match="control:missing_compatibility_output",
    ) as raised:
        await ResearchStrategyPlanningService(model_factory=lambda: model).propose(
            objective="选择控制方案。",
            requirement=requirement,
            scope_modules=(module,),
            research_depth="deep",
        )

    assert bound.ainvoke.await_count == 2
    assert "control:missing_distinct_research_lenses" in str(raised.value)
