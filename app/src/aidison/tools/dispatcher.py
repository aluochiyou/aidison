"""The single authorization choke point for LangGraph tool intentions.

This R3-01 slice intentionally stops after deterministic authorization.  It
does not call an adapter, reserve budget, retry, or own Artifact persistence;
those physical-execution concerns attach to the returned authorized invocation
in later R3 tickets.  No model-supplied input can carry runtime authority.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope


class ToolEffectClass(StrEnum):
    NONE = "none"
    READ = "read"
    EXTERNAL_WRITE = "external_write"


class ToolRiskTier(StrEnum):
    T0 = "t0"
    T1 = "t1"
    T2 = "t2"


class ToolDispatchDenied(RuntimeError):
    """Fail-closed authorization or schema refusal before a physical call."""


class ToolSpecRevision(BaseModel):
    """Immutable model-visible and server-enforced definition of one tool revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    revision: int = Field(ge=1)
    purpose: str = Field(min_length=1, max_length=2_000)
    tool_class: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    effect_class: ToolEffectClass
    risk_tier: ToolRiskTier
    input_schema_ref: str = Field(min_length=1, max_length=500)
    output_schema_ref: str = Field(min_length=1, max_length=500)
    definition_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def owns_a_content_identity(self) -> ToolSpecRevision:
        payload = self.model_dump(mode="json", exclude={"definition_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = sha256(canonical.encode()).hexdigest()
        if self.definition_hash is not None and self.definition_hash != expected:
            raise ValueError("ToolSpec definition_hash does not match definition")
        object.__setattr__(self, "definition_hash", expected)
        return self


class ToolCallIntent(BaseModel):
    """The complete untrusted model input; authority fields are intentionally absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,160}$")
    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    revision: int = Field(ge=1)
    arguments: dict[str, Any]
    purpose_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")


class ToolProfileGrant(BaseModel):
    """Run-frozen Capability Profile portion relevant to tools."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_ref: str = Field(min_length=1, max_length=500)
    allowed_tool_ids: tuple[str, ...] = Field(max_length=64)
    allowed_tool_classes: tuple[str, ...] = Field(max_length=64)
    allowed_effect_classes: tuple[ToolEffectClass, ...] = Field(max_length=3)


class ToolDispatchPolicy(BaseModel):
    """Trusted current policy and infrastructure availability projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_tool_classes: tuple[str, ...] = Field(max_length=64)
    allowed_effect_classes: tuple[ToolEffectClass, ...] = Field(max_length=3)
    available_risk_tiers: tuple[ToolRiskTier, ...] = Field(max_length=3)


class DispatchAuthority(BaseModel):
    """Trusted runtime inputs required to authorize one ToolCallIntent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: TaskEnvelope
    grant: ExecutionGrant
    profile: ToolProfileGrant
    policy: ToolDispatchPolicy
    deadline: datetime

    @model_validator(mode="after")
    def has_a_timezone_aware_deadline(self) -> DispatchAuthority:
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("dispatch deadline must be timezone-aware")
        return self


class AuthorizedToolInvocation(BaseModel):
    """A canonical logical invocation ready for the later physical-call kernel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    generation: int = Field(ge=1)
    tool_id: str
    revision: int = Field(ge=1)
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_arguments: dict[str, Any]
    logical_invocation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    logical_idempotency_key: str = Field(min_length=1, max_length=500)


class _RegisteredTool:
    def __init__(self, spec: ToolSpecRevision, input_model: type[BaseModel]) -> None:
        self.spec = spec
        self.input_model = input_model


class ToolRegistry:
    """In-memory immutable registry; it knows definitions, not per-task permission."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], _RegisteredTool] = {}

    def register(self, spec: ToolSpecRevision, *, input_model: type[BaseModel]) -> None:
        if input_model.model_config.get("extra") != "forbid":
            raise ValueError("ToolRegistry input model must forbid undeclared arguments")
        key = (spec.tool_id, spec.revision)
        current = self._definitions.get(key)
        if current is not None:
            if current.spec != spec or current.input_model is not input_model:
                raise ValueError("ToolRegistry identity conflicts with a different definition")
            return
        self._definitions[key] = _RegisteredTool(spec, input_model)

    def resolve(self, *, tool_id: str, revision: int) -> _RegisteredTool:
        resolved = self._definitions.get((tool_id, revision))
        if resolved is None:
            raise ToolDispatchDenied("Tool Registry does not contain requested exact revision")
        return resolved


class ToolDispatcher:
    """Compute Registry∩Profile∩Task∩Grant∩Policy∩availability exactly once."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def authorize(
        self,
        intent: ToolCallIntent,
        *,
        authority: DispatchAuthority,
    ) -> AuthorizedToolInvocation:
        self._validate_grant(authority)
        resolved = self._registry.resolve(tool_id=intent.tool_id, revision=intent.revision)
        spec = resolved.spec
        self._validate_intersection(spec, authority)
        try:
            canonical_arguments = resolved.input_model.model_validate(intent.arguments).model_dump(
                mode="json"
            )
        except ValidationError as exc:
            raise ToolDispatchDenied("ToolCallIntent fails exact input schema validation") from exc
        logical_id = self._logical_invocation_id(intent, authority, spec, canonical_arguments)
        definition_hash = spec.definition_hash
        assert definition_hash is not None
        return AuthorizedToolInvocation(
            run_id=authority.task.run_id,
            task_id=authority.task.id,
            attempt_id=authority.grant.attempt_id,
            generation=authority.grant.generation,
            tool_id=spec.tool_id,
            revision=spec.revision,
            definition_hash=definition_hash,
            canonical_arguments=canonical_arguments,
            logical_invocation_id=logical_id,
            logical_idempotency_key=(
                f"{authority.grant.idempotency_prefix}:{spec.tool_id}:{spec.revision}:{logical_id}"
            ),
        )

    @staticmethod
    def _validate_grant(authority: DispatchAuthority) -> None:
        if authority.grant.task_id != authority.task.id:
            raise ToolDispatchDenied("ExecutionGrant does not belong to TaskEnvelope")
        if datetime.now(UTC) >= authority.deadline:
            raise ToolDispatchDenied("ExecutionGrant deadline has elapsed")

    @staticmethod
    def _validate_intersection(spec: ToolSpecRevision, authority: DispatchAuthority) -> None:
        if spec.tool_id not in authority.task.allowed_tool_ids:
            raise ToolDispatchDenied("TaskEnvelope does not allow requested tool")
        if spec.tool_id not in authority.profile.allowed_tool_ids:
            raise ToolDispatchDenied("Capability Profile does not allow requested tool")
        if spec.tool_class not in authority.profile.allowed_tool_classes:
            raise ToolDispatchDenied("Capability Profile does not allow requested tool class")
        if spec.effect_class not in authority.profile.allowed_effect_classes:
            raise ToolDispatchDenied("Capability Profile does not allow requested effect class")
        if spec.tool_class not in authority.policy.allowed_tool_classes:
            raise ToolDispatchDenied("Tool Dispatch Policy does not allow requested tool class")
        if spec.effect_class not in authority.policy.allowed_effect_classes:
            raise ToolDispatchDenied("Tool Dispatch Policy does not allow requested effect class")
        if spec.risk_tier not in authority.policy.available_risk_tiers:
            raise ToolDispatchDenied("requested tool risk tier is unavailable")

    @staticmethod
    def _logical_invocation_id(
        intent: ToolCallIntent,
        authority: DispatchAuthority,
        spec: ToolSpecRevision,
        canonical_arguments: dict[str, Any],
    ) -> str:
        payload = {
            "run_id": str(authority.task.run_id),
            "task_id": str(authority.task.id),
            "attempt_id": str(authority.grant.attempt_id),
            "generation": authority.grant.generation,
            "tool_call_id": intent.tool_call_id,
            "purpose_code": intent.purpose_code,
            "tool_definition_hash": spec.definition_hash,
            "arguments": canonical_arguments,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()


__all__ = [
    "AuthorizedToolInvocation",
    "DispatchAuthority",
    "ToolCallIntent",
    "ToolDispatchDenied",
    "ToolDispatchPolicy",
    "ToolDispatcher",
    "ToolEffectClass",
    "ToolProfileGrant",
    "ToolRegistry",
    "ToolRiskTier",
    "ToolSpecRevision",
]
