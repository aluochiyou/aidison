"""Same-binding resume or successor-run recovery planning for AgentRun."""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.runtime.agent_runs import AdmittedCheckpointRef, AgentRun
from aidison.runtime.identity import RuntimeBinding


class RecoveryAction(StrEnum):
    RESUME = "resume"
    SUCCESSOR_RUN = "successor_run"


class RecoveryRequest(BaseModel):
    """Trusted Control inputs; private messages/checkpoint state are absent by design."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: AgentRun
    requested_binding: RuntimeBinding
    admitted_result_refs: tuple[str, ...] = Field(max_length=256)
    unadmitted_result_refs: tuple[str, ...] = Field(default=(), max_length=256)


class SuccessorTransferManifest(BaseModel):
    """A re-admission candidate list, never a checkpoint/state copy protocol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    predecessor_run_id: UUID
    predecessor_basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    admitted_result_refs: tuple[str, ...] = Field(max_length=256)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class SuccessorRunPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    successor_run_id: UUID
    runtime_binding: RuntimeBinding
    transfer_manifest: SuccessorTransferManifest


class RecoveryDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: RecoveryAction
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=8)
    resume_checkpoint: AdmittedCheckpointRef | None = None
    successor: SuccessorRunPlan | None = None

    @model_validator(mode="after")
    def action_has_one_recovery_path(self) -> RecoveryDecision:
        if self.action is RecoveryAction.RESUME:
            if self.resume_checkpoint is None or self.successor is not None:
                raise ValueError("resume recovery requires only an admitted checkpoint")
        elif self.successor is None or self.resume_checkpoint is not None:
            raise ValueError("successor recovery requires only a successor plan")
        return self


def plan_recovery(value: RecoveryRequest) -> RecoveryDecision:
    """Never interpret physical latest; resume only an exact admitted compatible anchor."""

    checkpoint = value.run.admitted_checkpoint
    binding = value.run.runtime_binding
    if (
        checkpoint is not None
        and binding == value.requested_binding
        and checkpoint.thread_id == value.run.thread_id
        and checkpoint.graph_revision == binding.graph_revision
        and checkpoint.state_schema_version == binding.state_schema_version
    ):
        return RecoveryDecision(
            action=RecoveryAction.RESUME,
            reason_codes=("admitted_checkpoint_matches_pinned_binding",),
            resume_checkpoint=checkpoint,
        )
    admitted_refs = tuple(sorted(set(value.admitted_result_refs)))
    manifest_values = {
        "predecessor_run_id": str(value.run.id),
        "predecessor_basis_hash": value.run.basis_hash,
        "admitted_result_refs": admitted_refs,
    }
    manifest = SuccessorTransferManifest(
        predecessor_run_id=value.run.id,
        predecessor_basis_hash=value.run.basis_hash,
        admitted_result_refs=admitted_refs,
        manifest_hash=sha256(
            json.dumps(manifest_values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    return RecoveryDecision(
        action=RecoveryAction.SUCCESSOR_RUN,
        reason_codes=("incompatible_or_missing_admitted_checkpoint",),
        successor=SuccessorRunPlan(
            successor_run_id=uuid4(),
            runtime_binding=value.requested_binding,
            transfer_manifest=manifest,
        ),
    )


__all__ = [
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryRequest",
    "SuccessorRunPlan",
    "SuccessorTransferManifest",
    "plan_recovery",
]
