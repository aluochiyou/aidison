"""Shared application service for reviewable, AI-proposed research strategies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Sequence
from typing import Literal
from uuid import UUID

from langchain_core.language_models import BaseChatModel

from aidison.agents.research_strategy import (
    JsonModeResearchStrategyPlanner,
    ResearchStrategyOutputValidationError,
    ResearchStrategyPlanner,
)
from aidison.domain.models import (
    Module,
    RequirementRevision,
    ResearchStrategyProposal,
    ResearchStrategyTask,
)


class ResearchStrategyPlanningError(ValueError):
    """The model output cannot become a bounded, reviewable strategy."""

    def __init__(self, message: str, *, diagnostics: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class ResearchStrategyPlanningService:
    """Plan research from approved facts without granting execution authority."""

    def __init__(
        self,
        *,
        model_factory: Callable[[], BaseChatModel] | None = None,
        planner_factory: Callable[[], ResearchStrategyPlanner] | None = None,
    ) -> None:
        if (model_factory is None) == (planner_factory is None):
            raise ValueError("provide exactly one research strategy planner factory")
        if planner_factory is not None:
            self._planner_factory = planner_factory
        else:
            assert model_factory is not None
            self._planner_factory = lambda: JsonModeResearchStrategyPlanner(model_factory())

    async def propose(
        self,
        *,
        objective: str,
        requirement: RequirementRevision,
        scope_modules: Sequence[Module],
        research_depth: Literal["focused", "standard", "deep"] = "standard",
        source_strategy: Literal["primary", "independent", "official", "mixed"] | None = None,
        planning_mode: Literal["monolithic", "hierarchical"] = "monolithic",
        planning_max_concurrency: int = 3,
    ) -> ResearchStrategyProposal:
        if not scope_modules:
            raise ResearchStrategyPlanningError("research strategy scope must not be empty")
        if planning_max_concurrency < 1:
            raise ResearchStrategyPlanningError("planning concurrency must be positive")
        if planning_mode == "hierarchical" and research_depth == "deep" and len(scope_modules) > 1:
            return await self._propose_hierarchical(
                objective=objective,
                requirement=requirement,
                scope_modules=scope_modules,
                source_strategy=source_strategy,
                planning_max_concurrency=planning_max_concurrency,
            )
        return await self._propose_complete(
            objective=objective,
            requirement=requirement,
            scope_modules=scope_modules,
            research_depth=research_depth,
            source_strategy=source_strategy,
            strategy_generation_mode="final",
        )

    async def _propose_complete(
        self,
        *,
        objective: str,
        requirement: RequirementRevision,
        scope_modules: Sequence[Module],
        research_depth: Literal["focused", "standard", "deep"],
        source_strategy: Literal["primary", "independent", "official", "mixed"] | None,
        strategy_generation_mode: Literal["final", "global_skeleton", "module_detail"],
        global_strategy_seed: dict[str, object] | None = None,
    ) -> ResearchStrategyProposal:
        scope_ids = tuple(item.id for item in scope_modules)
        context_value = self._planning_context(
            objective=objective,
            requirement=requirement,
            scope_modules=scope_modules,
            research_depth=research_depth,
            source_strategy=source_strategy,
            strategy_generation_mode=strategy_generation_mode,
            global_strategy_seed=global_strategy_seed,
        )
        context = json.dumps(context_value, ensure_ascii=False, sort_keys=True)
        planner = self._planner_factory()
        strategy = await self._invoke_planner(planner=planner, context=context)
        self._validate_scope(
            strategy=strategy,
            scope_ids=scope_ids,
            source_strategy=source_strategy,
        )
        if research_depth == "deep" and strategy_generation_mode != "global_skeleton":
            issues = self._deep_strategy_review(
                strategy=strategy,
                requirement=requirement,
                scope_modules=scope_modules,
                require_global_decision_notes=strategy_generation_mode != "module_detail",
            )
            if issues:
                strategy = await self._invoke_planner(
                    planner=planner,
                    context=json.dumps(
                        {
                            **context_value,
                            "strategy_review_feedback": {
                                "issues": issues,
                                "instruction": (
                                    "Revise the complete strategy to address every listed issue "
                                    "without changing scope."
                                ),
                            },
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    repair=True,
                )
                self._validate_scope(
                    strategy=strategy,
                    scope_ids=scope_ids,
                    source_strategy=source_strategy,
                )
                remaining_issues = self._deep_strategy_review(
                    strategy=strategy,
                    requirement=requirement,
                    scope_modules=scope_modules,
                    require_global_decision_notes=strategy_generation_mode != "module_detail",
                )
                if remaining_issues:
                    raise ResearchStrategyPlanningError(
                        "deep research strategy did not cover required lenses: "
                        + ", ".join(remaining_issues),
                        diagnostics=tuple(
                            f"deep_review:{issue}" for issue in remaining_issues
                        ),
                    )
        return strategy

    def _planning_context(
        self,
        *,
        objective: str,
        requirement: RequirementRevision,
        scope_modules: Sequence[Module],
        research_depth: Literal["focused", "standard", "deep"],
        source_strategy: Literal["primary", "independent", "official", "mixed"] | None,
        strategy_generation_mode: Literal["final", "global_skeleton", "module_detail"],
        global_strategy_seed: dict[str, object] | None,
    ) -> dict[str, object]:
        return {
            "objective": objective,
            "research_depth": research_depth,
            "strategy_generation_mode": strategy_generation_mode,
            "source_strategy_preference": source_strategy,
            "global_strategy_seed": global_strategy_seed,
            "planning_guidance": {
                    "focused": (
                        "Use the minimum task set that can answer the explicit objective; "
                        "do not split routine work merely to create more agents."
                    ),
                    "standard": (
                        "Separate materially different evidence, compatibility, or trade-off "
                        "questions when that makes the review clearer."
                    ),
                    "deep": (
                        "For each genuinely complex module, consider distinct constraint, "
                        "compatibility, trade-off, and risk tasks with useful dependencies. "
                        "Do not create cosmetic tasks or expand outside the approved scope."
                    ),
            }[research_depth],
            "requirement": {
                "goal": requirement.goal,
                "hard_constraints": requirement.hard_constraints,
                "preferences": requirement.preferences,
                "available_resources": requirement.available_resources,
                "usage_context": requirement.usage_context,
                "budget_context": requirement.budget_context,
                "skill_context": requirement.skill_context,
                "unknowns": requirement.unknowns,
            },
            "allowed_scope": [
                {
                    "id": str(item.id),
                    "key": item.key,
                    "name": item.name,
                    "responsibility": item.responsibility,
                    # These module-local facts were approved together with
                    # the blueprint.  Supplying only a boolean complexity
                    # hint made the planner know that it should split work
                    # without telling it *what* had to be investigated.
                    "acceptance": list(item.acceptance),
                    "open_questions": list(item.open_questions),
                    "dependencies": [str(dependency) for dependency in item.dependency_ids],
                    # This is an explainable planning signal, not a complexity
                    # score the model may reinterpret.  A module with a real
                    # interface dependency, several acceptance conditions or
                    # an explicit module-level unknown should not collapse all
                    # its deep-research work into one opaque model turn.
                    "deep_decomposition_required": _requires_deep_decomposition(item),
                }
                for item in scope_modules
            ],
        }

    @staticmethod
    async def _invoke_planner(
        *,
        planner: ResearchStrategyPlanner,
        context: str,
        repair: bool = False,
    ) -> ResearchStrategyProposal:
        try:
            return await planner.ainvoke(context)
        except ResearchStrategyOutputValidationError as exc:
            raise ResearchStrategyPlanningError(
                "research strategy repair returned invalid output"
                if repair
                else "research strategy returned invalid output",
                diagnostics=exc.diagnostics,
            ) from exc
        except ValueError as exc:
            raise ResearchStrategyPlanningError(
                "research strategy repair returned invalid output"
                if repair
                else "research strategy returned invalid output"
            ) from exc

    async def _propose_hierarchical(
        self,
        *,
        objective: str,
        requirement: RequirementRevision,
        scope_modules: Sequence[Module],
        source_strategy: Literal["primary", "independent", "official", "mixed"] | None,
        planning_max_concurrency: int,
    ) -> ResearchStrategyProposal:
        skeleton = await self._propose_complete(
            objective=objective,
            requirement=requirement,
            scope_modules=scope_modules,
            research_depth="deep",
            source_strategy=source_strategy,
            strategy_generation_mode="global_skeleton",
        )
        self._validate_global_skeleton(skeleton=skeleton, scope_modules=scope_modules)
        seed_by_module = {task.module_ids[0]: task for task in skeleton.tasks}
        complex_modules = tuple(
            module for module in scope_modules if _requires_deep_decomposition(module)
        )
        details = await self._plan_complex_modules(
            objective=objective,
            requirement=requirement,
            modules=complex_modules,
            source_strategy=skeleton.source_strategy,
            seed_by_module=seed_by_module,
            planning_max_concurrency=planning_max_concurrency,
        )
        strategy = self._merge_hierarchical_strategy(
            skeleton=skeleton,
            scope_modules=scope_modules,
            module_details=details,
        )
        issues = self._deep_strategy_review(
            strategy=strategy,
            requirement=requirement,
            scope_modules=scope_modules,
        )
        if issues:
            raise ResearchStrategyPlanningError(
                "hierarchical research strategy did not satisfy deep review",
                diagnostics=tuple(f"deep_review:{issue}" for issue in issues),
            )
        return strategy

    async def _plan_complex_modules(
        self,
        *,
        objective: str,
        requirement: RequirementRevision,
        modules: Sequence[Module],
        source_strategy: Literal["primary", "independent", "official", "mixed"],
        seed_by_module: dict[UUID, ResearchStrategyTask],
        planning_max_concurrency: int,
    ) -> dict[UUID, ResearchStrategyProposal]:
        semaphore = asyncio.Semaphore(planning_max_concurrency)

        async def plan_module(module: Module) -> tuple[UUID, ResearchStrategyProposal]:
            async with semaphore:
                seed = seed_by_module[module.id]
                return (
                    module.id,
                    await self._propose_complete(
                        objective=objective,
                        requirement=requirement,
                        scope_modules=(module,),
                        research_depth="deep",
                        source_strategy=source_strategy,
                        strategy_generation_mode="module_detail",
                        global_strategy_seed=seed.model_dump(mode="json"),
                    ),
                )

        results = await asyncio.gather(
            *(plan_module(module) for module in modules), return_exceptions=True
        )
        details: dict[UUID, ResearchStrategyProposal] = {}
        diagnostics: list[str] = []
        for module, result in zip(modules, results, strict=True):
            if isinstance(result, ResearchStrategyPlanningError):
                diagnostics.extend(
                    f"module:{module.key}:{diagnostic}"
                    for diagnostic in (result.diagnostics or ("invalid_output",))
                )
                continue
            if isinstance(result, BaseException):
                diagnostics.append(f"module:{module.key}:unexpected_planning_failure")
                continue
            module_id, strategy = result
            details[module_id] = strategy
        if diagnostics:
            raise ResearchStrategyPlanningError(
                "one or more module strategies were invalid",
                diagnostics=tuple(diagnostics),
            )
        return details

    @staticmethod
    def _validate_global_skeleton(
        *,
        skeleton: ResearchStrategyProposal,
        scope_modules: Sequence[Module],
    ) -> None:
        by_module = {
            module.id: tuple(task for task in skeleton.tasks if task.module_ids == (module.id,))
            for module in scope_modules
        }
        issues = [
            f"skeleton:{module.key}:expected_exactly_one_task"
            for module in scope_modules
            if len(by_module[module.id]) != 1
        ]
        issues.extend(
            f"skeleton:{task.task_key}:cross_module_dependency_not_allowed"
            for task in skeleton.tasks
            if task.depends_on_task_keys
        )
        if issues:
            raise ResearchStrategyPlanningError(
                "global strategy skeleton is invalid",
                diagnostics=tuple(issues),
            )

    @staticmethod
    def _merge_hierarchical_strategy(
        *,
        skeleton: ResearchStrategyProposal,
        scope_modules: Sequence[Module],
        module_details: dict[UUID, ResearchStrategyProposal],
    ) -> ResearchStrategyProposal:
        seed_by_module = {task.module_ids[0]: task for task in skeleton.tasks}
        task_groups: dict[UUID, tuple[ResearchStrategyTask, ...]] = {
            module.id: module_details.get(module.id, skeleton).tasks
            if module.id in module_details
            else (seed_by_module[module.id],)
            for module in scope_modules
        }
        remapped_groups: dict[UUID, tuple[ResearchStrategyTask, ...]] = {}
        for module in scope_modules:
            tasks = task_groups[module.id]
            task_key_map = {task.task_key: f"{module.key}.{task.task_key}" for task in tasks}
            remapped_groups[module.id] = tuple(
                task.model_copy(
                    update={
                        "task_key": task_key_map[task.task_key],
                        "depends_on_task_keys": tuple(
                            task_key_map[dependency]
                            for dependency in task.depends_on_task_keys
                        ),
                    }
                )
                for task in tasks
            )
        for module in scope_modules:
            upstream_module_ids = set(module.dependency_ids) & set(remapped_groups)
            if not upstream_module_ids:
                continue
            tasks = remapped_groups[module.id]
            starts = tuple(task for task in tasks if not task.depends_on_task_keys)
            upstream_terminals = tuple(
                task.task_key
                for upstream_id in sorted(upstream_module_ids, key=str)
                for task in remapped_groups[upstream_id]
                if task.task_key not in {
                    dependency
                    for candidate in remapped_groups[upstream_id]
                    for dependency in candidate.depends_on_task_keys
                }
            )
            if not starts or not upstream_terminals:
                continue
            remapped_groups[module.id] = tuple(
                task.model_copy(
                    update={
                        "depends_on_task_keys": tuple(
                            dict.fromkeys((*task.depends_on_task_keys, *upstream_terminals))
                        )
                    }
                )
                if task in starts
                else task
                for task in tasks
            )
        ordered_details = [
            module_details[module.id]
            for module in scope_modules
            if module.id in module_details
        ]
        return ResearchStrategyProposal(
            summary=skeleton.summary,
            decision_notes=_bounded_unique(
                item
                for strategy in (skeleton, *ordered_details)
                for item in strategy.decision_notes
            ),
            scope_module_ids=tuple(module.id for module in scope_modules),
            tasks=tuple(task for module in scope_modules for task in remapped_groups[module.id]),
            deferred_questions=_bounded_unique(
                item
                for strategy in (skeleton, *ordered_details)
                for item in strategy.deferred_questions
            ),
            risk_notes=_bounded_unique(
                item
                for strategy in (skeleton, *ordered_details)
                for item in strategy.risk_notes
            ),
            source_strategy=skeleton.source_strategy,
        )

    @staticmethod
    def _validate_scope(
        *,
        strategy: ResearchStrategyProposal,
        scope_ids: tuple[UUID, ...],
        source_strategy: Literal["primary", "independent", "official", "mixed"] | None,
    ) -> None:
        if set(strategy.scope_module_ids) != set(scope_ids):
            raise ResearchStrategyPlanningError(
                "research strategy changed the user-requested module scope"
            )
        if any(len(task.module_ids) != 1 for task in strategy.tasks):
            raise ResearchStrategyPlanningError(
                "research strategy tasks must target exactly one module"
            )
        task_module_ids = {task.module_ids[0] for task in strategy.tasks}
        if task_module_ids != set(scope_ids):
            raise ResearchStrategyPlanningError(
                "research strategy omits a selected module or targets an out-of-scope module"
            )
        if source_strategy is not None and strategy.source_strategy != source_strategy:
            raise ResearchStrategyPlanningError(
                "research strategy did not honor the requested source strategy"
            )

    @staticmethod
    def _deep_strategy_review(
        *,
        strategy: ResearchStrategyProposal,
        requirement: RequirementRevision,
        scope_modules: Sequence[Module],
        require_global_decision_notes: bool = True,
    ) -> list[str]:
        """Check declared deep-research lenses before a user can approve a plan.

        The output labels are not evidence and never authorize work.  They make
        the planner expose whether it considered a hard-constraint path and a
        module interface path, so a deep plan cannot silently collapse into a
        generic one-task-per-module outline.
        """

        tasks_by_module = {
            module.id: tuple(task for task in strategy.tasks if task.module_ids == (module.id,))
            for module in scope_modules
        }
        scoped_module_ids = set(tasks_by_module)
        issues: list[str] = []
        for module in sorted(scope_modules, key=lambda item: item.key):
            tasks = tasks_by_module[module.id]
            outputs = {output for task in tasks for output in task.expected_outputs}
            if not any(task.priority == "must" for task in tasks):
                issues.append(f"{module.key}:missing_must_task")
            if _requires_deep_decomposition(module) and len(tasks) < 2:
                issues.append(f"{module.key}:missing_deep_task_decomposition")
            lenses = {
                lens.casefold()
                for task in tasks
                for lens in task.research_lenses
            }
            if _requires_deep_decomposition(module) and len(lenses) < 2:
                issues.append(f"{module.key}:missing_distinct_research_lenses")
            if requirement.hard_constraints and "constraint" not in outputs:
                issues.append(f"{module.key}:missing_constraint_output")
            if module.dependency_ids and "compatibility" not in outputs:
                issues.append(f"{module.key}:missing_compatibility_output")
            scoped_dependencies = set(module.dependency_ids) & scoped_module_ids
            has_dependency_handoff = any(
                any(
                    upstream_task.module_ids == (dependency_id,)
                    for upstream_task in strategy.tasks
                    if upstream_task.task_key in task.depends_on_task_keys
                )
                for task in tasks
                for dependency_id in scoped_dependencies
            )
            if scoped_dependencies and not has_dependency_handoff:
                issues.append(f"{module.key}:missing_dependency_handoff")
        all_outputs = {output for task in strategy.tasks for output in task.expected_outputs}
        has_decision_context = bool(
            requirement.preferences
            or requirement.available_resources
            or requirement.usage_context.strip()
            or requirement.budget_context.strip()
        )
        if has_decision_context and "candidate" not in all_outputs:
            issues.append("strategy:missing_candidate_output_for_decision_context")
        if has_decision_context and require_global_decision_notes and not strategy.decision_notes:
            issues.append("strategy:missing_decision_notes_for_decision_context")
        if requirement.unknowns and not (
            strategy.deferred_questions or strategy.risk_notes
        ):
            issues.append("strategy:missing_deferred_question_or_risk_note")
        return issues


def _bounded_unique(items: Iterable[str]) -> tuple[str, ...]:
    """Keep review text bounded while preserving a deterministic first occurrence."""

    unique: dict[str, None] = {}
    for item in items:
        unique.setdefault(item, None)
        if len(unique) == 12:
            break
    return tuple(unique)


def _requires_deep_decomposition(module: Module) -> bool:
    """Identify module-local complexity that merits more than one deep task.

    The trigger is deliberately structural and conservative.  A generic
    project preference or budget alone does not multiply every module into
    several agents.  Module interfaces, multiple independently testable
    acceptance conditions, and explicit module questions are evidence that a
    single prompt would blur distinct investigations.
    """

    return bool(
        module.dependency_ids
        or len(tuple(item for item in module.acceptance if item.strip())) > 1
        or any(item.strip() for item in module.open_questions)
    )


__all__ = ["ResearchStrategyPlanningError", "ResearchStrategyPlanningService"]
