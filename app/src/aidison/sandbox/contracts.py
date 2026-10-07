"""Narrow, fixed-entrypoint contracts for T2 untrusted-content processing."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SandboxNetworkMode(StrEnum):
    NONE = "none"
    EGRESS_PROXY = "egress_proxy"


class SandboxInputArtifact(BaseModel):
    """One broker-authorized immutable input copied into `/input`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_ref: str = Field(pattern=r"^artifact\+sha256://[a-f0-9]{64}/.+", max_length=500)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)
    relative_path: str = Field(min_length=1, max_length=240)

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("sandbox input relative_path must not escape its workspace")
        return value


class SandboxResourcePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_millis: int = Field(ge=100, le=8_000)
    memory_mebibytes: int = Field(ge=64, le=32_768)
    pids_limit: int = Field(ge=1, le=512)
    fd_limit: int = Field(ge=32, le=16_384)
    wall_clock_seconds: int = Field(ge=1, le=3_600)
    output_max_bytes: int = Field(ge=1, le=2_000_000_000)
    output_max_files: int = Field(ge=1, le=1_024)


class SandboxTaskSpec(BaseModel):
    """Controller-only execution request; it has no shell, host path, or secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sandbox_task_id: UUID = Field(default_factory=uuid4)
    invocation_id: UUID
    run_id: UUID
    task_id: UUID
    producer_generation: int = Field(ge=1)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    sandbox_profile_ref: str = Field(pattern=r"^sandbox-profile://.+", max_length=500)
    image_digest: str = Field(pattern=r"^[^\s@]+@sha256:[a-f0-9]{64}$", max_length=500)
    entrypoint_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    arguments: dict[str, object]
    input_artifacts: tuple[SandboxInputArtifact, ...] = Field(max_length=64)
    output_schema_ref: str = Field(pattern=r"^schema://.+", max_length=500)
    network_mode: SandboxNetworkMode
    resource_policy: SandboxResourcePolicy
    secret_policy_ref: str = Field(pattern=r"^secret-policy://.+", max_length=500)
    deadline: datetime
    idempotency_key: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def task_inputs_are_unique_and_deadline_is_aware(self) -> SandboxTaskSpec:
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("SandboxTaskSpec deadline must be timezone-aware")
        paths = tuple(item.relative_path for item in self.input_artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("sandbox input paths must be unique")
        return self
