"""Checkpointed wrapper for one bounded deterministic ImpactGraph run."""

from __future__ import annotations

from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from aidison.impact.contracts import ImpactGraphState
from aidison.impact.execution import ImpactRunExecutor
from aidison.research.langgraph_contracts import AdmittedResultRef
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim


def build_impact_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any],
    executor: ImpactRunExecutor,
    run: AgentRun,
    claim: AgentRunClaim,
) -> Any:
    """Build a ref-only graph that materializes a deterministic Impact report."""

    async def materialize(state: ImpactGraphState) -> ImpactGraphState:
        if state.get("run_id") != str(run.id):
            raise ValueError("ImpactGraph state must carry the pinned AgentRun id")
        execution = await executor.execute(run=run, claim=claim)
        if execution.admission.admitted_ref is None:  # pragma: no cover - executor fixes ACCEPTED
            raise RuntimeError("accepted Impact result is missing its admitted reference")
        return {
            "impact_contract_ref": run.run_contract_ref or "",
            "change_set_ref": execution.report_manifest.change_set_ref,
            "structural_partition_ref": execution.structural_partition_ref,
            "change_plan_ref": execution.change_plan_ref,
            "steering_preview_ref": execution.steering_preview_ref,
            "report_manifest_ref": execution.result.artifact_ref,
            "admitted_result_refs": (
                AdmittedResultRef(
                    result_id=execution.result.id,
                    manifest_hash=execution.result.manifest_hash,
                    admitted_ref=execution.admission.admitted_ref,
                ),
            ),
        }

    builder = StateGraph(ImpactGraphState)
    builder.add_node("materialize", cast(Any, materialize))
    builder.add_edge(START, "materialize")
    builder.add_edge("materialize", END)
    return builder.compile(checkpointer=checkpointer, name="aidison_impact_graph_v1")


__all__ = ["build_impact_graph"]
