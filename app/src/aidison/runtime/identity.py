"""Immutable identities for LangGraph-owned AgentRun execution."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RuntimeFamily(StrEnum):
    """Execution semantics pinned when an AgentRun is created."""

    LEGACY = "legacy_runtime"
    LANGGRAPH_V1 = "langgraph_runtime_v1"


class RuntimeBinding(BaseModel):
    """The immutable runtime, graph, state, profile, and policy identity of a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_family: RuntimeFamily
    runtime_revision: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    graph_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    graph_revision: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    state_schema_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    profile_binding_ref: str = Field(min_length=1, max_length=500)
    policy_binding_ref: str = Field(min_length=1, max_length=500)


class RuntimeBindingConflict(ValueError):
    """Raised when a caller attempts to change a run's pinned runtime identity."""


class UnsupportedRuntimeBinding(ValueError):
    """Raised before claim when a worker cannot execute a pinned run."""


class WorkerRuntimeSupport(BaseModel):
    """Exact runtime bindings an orchestration worker is allowed to claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_revision: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    bindings: tuple[RuntimeBinding, ...] = Field(min_length=1)


def ensure_runtime_binding_unchanged(
    *,
    pinned: RuntimeBinding,
    requested: RuntimeBinding,
) -> RuntimeBinding:
    """Return the existing binding or reject any attempted identity change."""

    changed_fields = [
        field_name
        for field_name in RuntimeBinding.model_fields
        if getattr(pinned, field_name) != getattr(requested, field_name)
    ]
    if changed_fields:
        changed = ", ".join(changed_fields)
        raise RuntimeBindingConflict(f"runtime binding is immutable; changed fields: {changed}")
    return pinned


def ensure_worker_supports(
    *,
    binding: RuntimeBinding,
    support: WorkerRuntimeSupport,
) -> RuntimeBinding:
    """Reject unsupported runs before a worker obtains execution ownership."""

    if binding not in support.bindings:
        raise UnsupportedRuntimeBinding(
            "worker does not support pinned runtime binding: " f"{binding.model_dump_json()}"
        )
    return binding
