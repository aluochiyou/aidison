"""Shared contracts retained outside the LangGraph execution state.

The old Job/Attempt/Delegation runtime was removed in R7.  LangGraph task and
result contracts now live in :mod:`aidison.research.langgraph_contracts`; this
module contains only the small cross-cutting contracts still used by the
project, provider replay ledger, and frozen profile configuration.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_DELEGATION_WAVE_SIZE = 8


class RuntimeContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BudgetOperationKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"


class InvocationRecordingStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    AMBIGUOUS = "ambiguous"


class FaultInjectionMode(StrEnum):
    NONE = "none"
    BEFORE_DISPATCH_TIMEOUT = "before_dispatch_timeout"
    AFTER_EFFECT_UNKNOWN = "after_effect_unknown"


class CoordinationMode(StrEnum):
    DECOMPOSE = "decompose"
    VERIFY = "verify"
    REPLICATE = "replicate"
    ESCALATE = "escalate"


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    CONFIGURATION = "configuration"
    PERMISSION = "permission"
    VALIDATION = "validation"
    EVIDENCE_CONFLICT = "evidence_conflict"
    BUDGET = "budget"
    UNKNOWN_EFFECT = "unknown_effect"


class InvocationRecording(RuntimeContract):
    """Secret-free durable evidence for one model or tool invocation."""

    project_id: UUID
    agent_run_id: UUID
    producer_attempt_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=300)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: BudgetOperationKind
    provider: str = Field(min_length=1, max_length=100)
    operation_name: str = Field(min_length=1, max_length=200)
    status: InvocationRecordingStatus
    response_artifact_ref: str | None = Field(default=None, max_length=500)
    failure_class: FailureClass | None = None

    @model_validator(mode="after")
    def recording_is_replay_safe(self) -> InvocationRecording:
        if self.status is InvocationRecordingStatus.SUCCEEDED:
            if self.failure_class is not None or self.response_artifact_ref is None:
                raise ValueError("successful recording requires response artifact and no failure")
        elif self.status is InvocationRecordingStatus.AMBIGUOUS and (
            self.failure_class is not FailureClass.UNKNOWN_EFFECT
        ):
            raise ValueError("ambiguous recording requires unknown_effect classification")
        elif self.status is InvocationRecordingStatus.PENDING and (
            self.failure_class is not None or self.response_artifact_ref is not None
        ):
            raise ValueError("pending recording cannot carry a response or failure")
        return self


class FaultInjection(RuntimeContract):
    """Explicit local test instruction; never inferred from production input."""

    mode: FaultInjectionMode = FaultInjectionMode.NONE
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=300)
