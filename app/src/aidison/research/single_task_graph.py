"""LangGraph wrapper for R1's bounded single-task research execution."""

from __future__ import annotations

from typing import Any, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aidison.application.single_task_research import SingleTaskResearchExecutor
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


class SingleTaskResearchGraphState(TypedDict, total=False):
    run_id: str
    question: str
    raw_artifact_ref: str | None
    admitted_result_ref: str
    proposal_manifest_ref: str
    proposal_manifest_hash: str


class _SingleTaskCapabilityAgent:
    """R1 executor adapter; one instance is created for each task invocation."""

    def __init__(
        self,
        *,
        executor: SingleTaskResearchExecutor,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
    ) -> None:
        self._executor = executor
        self._run = run
        self._claim = claim
        self._task = task
        self._grant = grant

    async def execute(self, *, context: PrivateCapabilityContext) -> CapabilityPublicResult:
        if context.run_id != self._run.id or context.task_id != self._task.id:
            raise ValueError("private capability context does not match the pinned task")
        execution = await self._executor.execute(
            run=self._run,
            claim=self._claim,
            task=self._task,
            grant=self._grant,
            question=context.task_instruction,
        )
        return CapabilityPublicResult(
            result_ref=execution.proposal.artifact_ref,
            result_manifest_hash=execution.proposal.manifest_hash,
            raw_output_ref=execution.raw_artifact_ref,
            admission_ref=execution.admission.admitted_ref,
            usage_ref=execution.result.usage_ref,
            failure_ref=execution.result.failure_ref,
            unresolved_refs=execution.result.unresolved_refs,
        )


class _SingleTaskCapabilityAgentFactory(CapabilityAgentFactory):
    def __init__(
        self,
        *,
        executor: SingleTaskResearchExecutor,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
    ) -> None:
        self._executor = executor
        self._run = run
        self._claim = claim
        self._task = task
        self._grant = grant

    def create(self, *, context: PrivateCapabilityContext) -> _SingleTaskCapabilityAgent:
        return _SingleTaskCapabilityAgent(
            executor=self._executor,
            run=self._run,
            claim=self._claim,
            task=self._task,
            grant=self._grant,
        )


def build_single_task_research_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any],
    executor: SingleTaskResearchExecutor,
    run: AgentRun,
    claim: AgentRunClaim,
    task: TaskEnvelope,
    grant: ExecutionGrant,
) -> Any:
    """Build a per-invocation graph with private execution captured in a node."""

    capability_subgraph = build_capability_subgraph(
        assembler=TaskScopedContextAssembler(),
        agent_factory=_SingleTaskCapabilityAgentFactory(
            executor=executor,
            run=run,
            claim=claim,
            task=task,
            grant=grant,
        ),
    )

    async def research(state: SingleTaskResearchGraphState) -> SingleTaskResearchGraphState:
        if state.get("run_id") != str(run.id):
            raise ValueError("ResearchGraph state must carry the pinned AgentRun id")
        result = await capability_subgraph.execute(
            invocation=CapabilityInvocation(
                task=task,
                task_instruction=state["question"],
                profile_ref=run.runtime_binding.profile_binding_ref,
                canonical_fact_refs=(),
                admitted_material_refs=(),
                advisory_refs=(),
            )
        )
        if result.admission_ref is None:
            raise RuntimeError("single-task capability did not return required public references")
        if result.result_manifest_hash is None:
            raise RuntimeError("single-task capability did not return a manifest hash")
        return {
            "raw_artifact_ref": result.raw_output_ref,
            "admitted_result_ref": result.admission_ref,
            "proposal_manifest_ref": result.result_ref,
            "proposal_manifest_hash": result.result_manifest_hash,
        }

    def await_user_decision(state: SingleTaskResearchGraphState) -> SingleTaskResearchGraphState:
        interrupt(
            {
                "kind": "research_proposal_review",
                "proposal_manifest_ref": state["proposal_manifest_ref"],
                "proposal_manifest_hash": state["proposal_manifest_hash"],
            }
        )
        return {}

    builder = StateGraph(SingleTaskResearchGraphState)
    builder.add_node("research", cast(Any, research))
    builder.add_node("await_user_decision", cast(Any, await_user_decision))
    builder.add_edge(START, "research")
    builder.add_edge("research", "await_user_decision")
    builder.add_edge("await_user_decision", END)
    return builder.compile(checkpointer=checkpointer, name="aidison_single_task_research_v1")
