from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from aidison.runtime.contracts import MAX_DELEGATION_WAVE_SIZE
from aidison.runtime.planning import (
    OrchestrationPlanRevision,
    PlanningContract,
    TaskNode,
)


class ResearchMode(StrEnum):
    ATOM = "atom"
    DEEP = "deep"
    WIDE = "wide"
    ENTITY_COLLECT = "entity_collect"


class GapStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class ResearchGap(PlanningContract):
    """A bounded, deduplicated research gap used as immutable replanning input."""

    root_job_id: UUID
    task_logical_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    plan_revision: int = Field(ge=1)
    source_result_id: UUID | None = None
    source_result_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    gap_hash: str = Field(default="", pattern=r"^[a-f0-9]{64}$")
    category: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4_000)
    module_refs: tuple[str, ...] = Field(default=(), max_length=8)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=16)
    status: GapStatus = GapStatus.OPEN
    priority: int = Field(default=0, ge=0, le=100)
    bound: int = Field(default=1, ge=1, le=4)

    @model_validator(mode="after")
    def validate_lineage(self) -> ResearchGap:
        if self.source_result_hash is not None and self.source_result_id is None:
            raise ValueError("source_result_hash requires source_result_id")
        expected_hash = canonical_gap_hash(self)
        if self.gap_hash and self.gap_hash != expected_hash:
            raise ValueError("gap_hash does not match canonical gap payload")
        object.__setattr__(self, "gap_hash", expected_hash)
        return self


def canonical_gap_hash(gap: ResearchGap) -> str:
    payload = gap.model_dump(
        mode="json",
        exclude={"gap_hash", "status", "priority"},
    )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


def _value(item: Any, key: str) -> Any:
    if isinstance(item, Mapping):
        return item[key]
    return getattr(item, key)


def build_research_shadow_plan(
    *,
    root_job_id: str,
    basis_hash: str,
    modules: Sequence[Any],
    profile_id: str,
    profile_revision: int,
    planner_profile_id: str,
    planner_profile_revision: int,
    budget_ref: str,
    max_shards: int = MAX_DELEGATION_WAVE_SIZE,
) -> OrchestrationPlanRevision:
    """Build a deterministic bounded N-way projection for one research wave."""

    if not modules:
        raise ValueError("research shadow plan requires at least one module")
    if not 1 <= max_shards <= MAX_DELEGATION_WAVE_SIZE:
        raise ValueError(
            f"research shadow plan max_shards must be between 1 and {MAX_DELEGATION_WAVE_SIZE}"
        )
    shard_count = min(len(modules), max_shards)
    shards = tuple(modules[index::shard_count] for index in range(shard_count))
    nodes = tuple(
        TaskNode(
            logical_key=f"research.shard-{index + 1}",
            objective="核验模块：" + "、".join(str(_value(item, "key")) for item in shard),
            mode=ResearchMode.WIDE if len(shard) > 1 else ResearchMode.ATOM,
            role_key="research-worker",
            profile_id=profile_id,
            profile_revision=profile_revision,
            budget_ref=budget_ref,
            depth=0,
            input_refs=tuple(f"module://{_value(item, 'id')}" for item in shard),
            success_criteria=("每个模块至少返回一项可追溯的证据或明确缺口",),
            stop_criteria=("达到当前固定 wave 的模块覆盖边界",),
        )
        for index, shard in enumerate(shards)
        if shard
    )
    return OrchestrationPlanRevision(
        root_job_id=root_job_id,
        revision=1,
        basis_hash=basis_hash,
        reason="bounded N-way research.parallel wave 的 durable shadow projection",
        planner_profile_id=planner_profile_id,
        planner_profile_revision=planner_profile_revision,
        nodes=nodes,
    )
