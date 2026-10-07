from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

from aidison.runtime.agent_runs import (
    AdmittedCheckpointRef,
    AgentRun,
    AgentRunKind,
    execution_checkpoint_thread_id,
)
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.runtime.recovery import RecoveryAction, RecoveryRequest, plan_recovery


def _binding(*, graph: str = "research-v1", state: str = "research-state-v1") -> RuntimeBinding:
    return RuntimeBinding(
        runtime_family=RuntimeFamily.LANGGRAPH_V1,
        runtime_revision="runtime-v1",
        graph_key="research",
        graph_revision=graph,
        state_schema_version=state,
        profile_binding_ref="profile://research/1",
        policy_binding_ref="policy://research/1",
    )


def _run() -> AgentRun:
    return AgentRun(
        project_id=uuid4(),
        kind=AgentRunKind.RESEARCH,
        idempotency_key="recovery-source",
        basis_hash=sha256(b"basis").hexdigest(),
        basis_project_revision=1,
        runtime_binding=_binding(),
        thread_id="run-source",
        admitted_checkpoint=AdmittedCheckpointRef(
            thread_id=execution_checkpoint_thread_id(
                logical_thread_id="run-source",
                generation=1,
            ),
            checkpoint_id="checkpoint-1",
            graph_revision="research-v1",
            state_schema_version="research-state-v1",
            generation=1,
        ),
    )


def test_same_runtime_and_admitted_checkpoint_resumes_exact_anchor() -> None:
    decision = plan_recovery(
        RecoveryRequest(
            run=_run(), requested_binding=_binding(), admitted_result_refs=("admitted://r/1",)
        )
    )
    assert decision.action is RecoveryAction.RESUME
    assert decision.resume_checkpoint is not None
    assert decision.successor is None


def test_incompatible_state_creates_successor_without_copying_checkpoint_or_private_state() -> None:
    source = _run()
    decision = plan_recovery(
        RecoveryRequest(
            run=source,
            requested_binding=_binding(graph="research-v2", state="research-state-v2"),
            admitted_result_refs=("admitted://r/1", "admitted://r/2"),
        )
    )
    assert decision.action is RecoveryAction.SUCCESSOR_RUN
    assert decision.resume_checkpoint is None
    assert decision.successor is not None
    assert decision.successor.transfer_manifest.predecessor_run_id == source.id
    assert decision.successor.transfer_manifest.admitted_result_refs == (
        "admitted://r/1",
        "admitted://r/2",
    )
    assert "checkpoint" not in decision.successor.transfer_manifest.model_dump_json()


def test_checkpoint_from_another_thread_creates_a_successor_run() -> None:
    source = _run().model_copy(
        update={
            "admitted_checkpoint": AdmittedCheckpointRef(
                thread_id="another-run-thread",
                checkpoint_id="checkpoint-1",
                graph_revision="research-v1",
                state_schema_version="research-state-v1",
                generation=1,
            )
        }
    )

    decision = plan_recovery(
        RecoveryRequest(
            run=source,
            requested_binding=_binding(),
            admitted_result_refs=("admitted://r/1",),
        )
    )

    assert decision.action is RecoveryAction.SUCCESSOR_RUN
    assert decision.resume_checkpoint is None
    assert decision.successor is not None
    assert decision.successor.transfer_manifest.predecessor_run_id == source.id


def test_unadmitted_material_cannot_enter_successor_transfer() -> None:
    source = _run()
    decision = plan_recovery(
        RecoveryRequest(
            run=source,
            requested_binding=_binding(graph="research-v2", state="research-state-v2"),
            admitted_result_refs=("admitted://r/1",),
            unadmitted_result_refs=("candidate://private-draft",),
        )
    )
    assert decision.successor is not None
    assert "private-draft" not in decision.successor.transfer_manifest.model_dump_json()
