"""The first real checkpointed LangGraph used to prove runtime ownership."""

from __future__ import annotations

from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from typing_extensions import TypedDict

from aidison.runtime.agent_runs import AdmittedCheckpointRef
from aidison.runtime.identity import RuntimeBinding


class MinimalCheckpointedState(TypedDict, total=False):
    """Deliberately small state; business state is not copied into a checkpoint."""

    run_id: str
    prompt: str
    phase: str
    decision: str
    approved_prompt: str


def thread_config(*, thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def admitted_checkpoint_config(ref: AdmittedCheckpointRef) -> dict[str, dict[str, str]]:
    """Build resume configuration from Control's anchor, never physical latest."""

    return {
        "configurable": {
            "thread_id": ref.thread_id,
            "checkpoint_ns": ref.checkpoint_ns,
            "checkpoint_id": ref.checkpoint_id,
        }
    }


def admitted_checkpoint_from_snapshot(
    *,
    snapshot: StateSnapshot,
    binding: RuntimeBinding,
    generation: int,
) -> AdmittedCheckpointRef:
    """Convert a LangGraph snapshot to the small recovery anchor Control owns."""

    configurable = snapshot.config.get("configurable", {})
    thread_id = configurable.get("thread_id")
    checkpoint_id = configurable.get("checkpoint_id")
    checkpoint_ns = configurable.get("checkpoint_ns", "")
    if not isinstance(thread_id, str) or not isinstance(checkpoint_id, str):
        raise ValueError("LangGraph snapshot is missing a durable thread/checkpoint identity")
    if not isinstance(checkpoint_ns, str):
        raise ValueError("LangGraph checkpoint namespace must be a string")
    return AdmittedCheckpointRef(
        thread_id=thread_id,
        checkpoint_ns=checkpoint_ns,
        checkpoint_id=checkpoint_id,
        graph_revision=binding.graph_revision,
        state_schema_version=binding.state_schema_version,
        generation=generation,
    )


def build_minimal_checkpointed_graph(*, checkpointer: BaseCheckpointSaver[Any]) -> Any:
    """Build a checkpoint → interrupt → explicit-resume → terminal proof graph."""

    def prepare(_: MinimalCheckpointedState) -> MinimalCheckpointedState:
        return {"phase": "waiting_for_decision"}

    def wait_for_decision(state: MinimalCheckpointedState) -> MinimalCheckpointedState:
        decision = interrupt(
            {
                "kind": "minimal_runtime_approval",
                "run_id": state["run_id"],
                "prompt": state["prompt"],
            }
        )
        return {"phase": "resumed", "decision": str(decision)}

    def finish(state: MinimalCheckpointedState) -> MinimalCheckpointedState:
        return {
            "phase": "completed",
            "approved_prompt": state["prompt"],
        }

    builder = StateGraph(MinimalCheckpointedState)
    # LangGraph's current type overload does not accept partial TypedDict
    # updates even though StateGraph accepts them at runtime.
    builder.add_node("prepare", cast(Any, prepare))
    builder.add_node("wait_for_decision", cast(Any, wait_for_decision))
    builder.add_node("finish", cast(Any, finish))
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "wait_for_decision")
    builder.add_edge("wait_for_decision", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer, name="aidison_minimal_runtime_v1")


__all__ = [
    "MinimalCheckpointedState",
    "admitted_checkpoint_config",
    "admitted_checkpoint_from_snapshot",
    "build_minimal_checkpointed_graph",
    "thread_config",
    "Command",
]
