"""LangGraph wrapper for one bounded Solution Composer task invocation."""

from __future__ import annotations

from typing import Any, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aidison.research.capability_subgraph import (
    CapabilityAgentFactory,
    CapabilityInvocation,
    CapabilityPublicResult,
    PrivateCapabilityContext,
    TaskScopedContextAssembler,
    build_capability_subgraph,
)
from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim
from aidison.solution.composition_executor import (
    SolutionCompositionExecutor,
    SolutionCompositionMaterial,
)
from aidison.solution.proposal_manifest import SolutionProposalFreezer


class SingleTaskSolutionGraphState(TypedDict, total=False):
    """Root state projection: no raw payload, element body, or private messages."""

    run_id: str
    raw_artifact_ref: str
    normalized_artifact_ref: str
    integration_artifact_ref: str
    verifier_plan_artifact_ref: str
    verifier_observation_refs: tuple[str, ...]
    proposal_manifest_ref: str
    proposal_readiness: str
    admitted_result_ref: str


class _SolutionCapabilityAgent:
    def __init__(
        self,
        *,
        executor: SolutionCompositionExecutor,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        material: SolutionCompositionMaterial,
        proposal_freezer: SolutionProposalFreezer,
    ) -> None:
        self._executor = executor
        self._run = run
        self._claim = claim
        self._task = task
        self._grant = grant
        self._material = material
        self._proposal_freezer = proposal_freezer

    async def execute(self, *, context: PrivateCapabilityContext) -> CapabilityPublicResult:
        if context.run_id != self._run.id or context.task_id != self._task.id:
            raise ValueError("private capability context does not match the pinned solution task")
        execution = await self._executor.execute(
            run=self._run,
            claim=self._claim,
            task=self._task,
            grant=self._grant,
            material=self._material,
        )
        frozen = await self._proposal_freezer.freeze(
            run=self._run,
            contract=self._material.contract,
            execution=execution,
        )
        return CapabilityPublicResult(
            result_ref=execution.normalized_artifact_ref,
            result_manifest_hash=execution.result.manifest_hash,
            raw_output_ref=execution.raw_artifact_ref,
            admission_ref=execution.admission.admitted_ref,
            unresolved_refs=execution.result.unresolved_refs,
            output_refs=(
                execution.integration_artifact_ref,
                execution.verifier_plan_artifact_ref,
                frozen.review_manifest.artifact_ref,
                *execution.verifier_observation_refs,
            ),
            control_outcome=frozen.proposal.readiness.value,
        )


class _SolutionCapabilityAgentFactory(CapabilityAgentFactory):
    def __init__(
        self,
        *,
        executor: SolutionCompositionExecutor,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        material: SolutionCompositionMaterial,
        proposal_freezer: SolutionProposalFreezer,
    ) -> None:
        self._executor = executor
        self._run = run
        self._claim = claim
        self._task = task
        self._grant = grant
        self._material = material
        self._proposal_freezer = proposal_freezer

    def create(self, *, context: PrivateCapabilityContext) -> _SolutionCapabilityAgent:
        return _SolutionCapabilityAgent(
            executor=self._executor,
            run=self._run,
            claim=self._claim,
            task=self._task,
            grant=self._grant,
            material=self._material,
            proposal_freezer=self._proposal_freezer,
        )


def build_single_task_solution_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any],
    executor: SolutionCompositionExecutor,
    run: AgentRun,
    claim: AgentRunClaim,
    task: TaskEnvelope,
    grant: ExecutionGrant,
    material: SolutionCompositionMaterial,
    proposal_freezer: SolutionProposalFreezer,
) -> Any:
    """Build one private-composition graph; proposal approval is a later R6-04 bridge."""

    capability_subgraph = build_capability_subgraph(
        assembler=TaskScopedContextAssembler(),
        agent_factory=_SolutionCapabilityAgentFactory(
            executor=executor,
            run=run,
            claim=claim,
            task=task,
            grant=grant,
            material=material,
            proposal_freezer=proposal_freezer,
        ),
    )

    async def compose(state: SingleTaskSolutionGraphState) -> SingleTaskSolutionGraphState:
        if state.get("run_id") != str(run.id):
            raise ValueError("SolutionGraph state must carry the pinned AgentRun id")
        result = await capability_subgraph.execute(
            invocation=CapabilityInvocation(
                task=task,
                task_instruction=material.instruction,
                profile_ref=run.runtime_binding.profile_binding_ref,
                canonical_fact_refs=task.input_refs,
                admitted_material_refs=material.contract.admitted_evidence_refs,
                advisory_refs=(),
            )
        )
        if (
            result.raw_output_ref is None
            or result.admission_ref is None
            or len(result.output_refs) < 3
            or result.control_outcome is None
        ):
            raise RuntimeError("solution capability did not return required public references")
        return {
            "raw_artifact_ref": result.raw_output_ref,
            "normalized_artifact_ref": result.result_ref,
            "integration_artifact_ref": result.output_refs[0],
            "verifier_plan_artifact_ref": result.output_refs[1],
            "verifier_observation_refs": result.output_refs[3:],
            "proposal_manifest_ref": result.output_refs[2],
            "proposal_readiness": result.control_outcome,
            "admitted_result_ref": result.admission_ref,
        }

    def route_after_composition(state: SingleTaskSolutionGraphState) -> str:
        return "await_user_decision" if state["proposal_readiness"] == "ready" else END

    def await_user_decision(state: SingleTaskSolutionGraphState) -> SingleTaskSolutionGraphState:
        interrupt(
            {
                "kind": "solution_proposal_review",
                "proposal_manifest_ref": state["proposal_manifest_ref"],
                "proposal_readiness": state["proposal_readiness"],
            }
        )
        return {}

    builder = StateGraph(SingleTaskSolutionGraphState)
    builder.add_node("compose", cast(Any, compose))
    builder.add_node("await_user_decision", cast(Any, await_user_decision))
    builder.add_edge(START, "compose")
    builder.add_conditional_edges("compose", route_after_composition)
    builder.add_edge("await_user_decision", END)
    return builder.compile(checkpointer=checkpointer, name="aidison_single_task_solution_v1")


__all__ = ["SingleTaskSolutionGraphState", "build_single_task_solution_graph"]
