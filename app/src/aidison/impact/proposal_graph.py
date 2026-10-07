"""LangGraph wrapper for the bounded Impact Analyst and human-review interruption."""

from __future__ import annotations

from typing import Any, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aidison.impact.proposal_execution import ImpactProposalExecutor
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim


class ImpactProposalGraphState(TypedDict, total=False):
    """Root state contains only durable artifact/admission references."""

    run_id: str
    report_manifest_ref: str
    raw_output_ref: str
    patch_candidate_ref: str
    proposal_manifest_ref: str
    admitted_report_ref: str
    admitted_candidate_ref: str
    proposal_readiness: str


def build_impact_proposal_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any],
    executor: ImpactProposalExecutor,
    run: AgentRun,
    claim: AgentRunClaim,
) -> Any:
    """Build a ref-only ImpactGraph that pauses before any canonical mutation."""

    async def materialize(state: ImpactProposalGraphState) -> ImpactProposalGraphState:
        if state.get("run_id") != str(run.id):
            raise ValueError("Impact proposal graph state must carry the pinned AgentRun id")
        execution = await executor.execute(run=run, claim=claim)
        if (
            execution.report.admission.admitted_ref is None
            or execution.admission.admitted_ref is None
        ):
            raise RuntimeError("accepted Impact results must carry admitted references")
        return {
            "report_manifest_ref": execution.report.result.artifact_ref,
            "raw_output_ref": execution.raw_output_ref,
            "patch_candidate_ref": execution.patch_candidate_ref,
            "proposal_manifest_ref": execution.proposal_manifest_ref,
            "admitted_report_ref": execution.report.admission.admitted_ref,
            "admitted_candidate_ref": execution.admission.admitted_ref,
            "proposal_readiness": "ready",
        }

    def await_user_decision(state: ImpactProposalGraphState) -> ImpactProposalGraphState:
        interrupt(
            {
                "kind": "impact_proposal_review",
                "proposal_manifest_ref": state["proposal_manifest_ref"],
                "proposal_readiness": state["proposal_readiness"],
            }
        )
        return {}

    builder = StateGraph(ImpactProposalGraphState)
    builder.add_node("materialize", cast(Any, materialize))
    builder.add_node("await_user_decision", cast(Any, await_user_decision))
    builder.add_edge(START, "materialize")
    builder.add_edge("materialize", "await_user_decision")
    builder.add_edge("await_user_decision", END)
    return builder.compile(checkpointer=checkpointer, name="aidison_impact_graph_v2")


__all__ = ["ImpactProposalGraphState", "build_impact_proposal_graph"]
