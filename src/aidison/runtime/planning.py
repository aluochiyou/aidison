from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanningContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskEdgeKind(StrEnum):
    DEPENDS_ON = "depends_on"
    EVIDENCE_FROM = "evidence_from"
    VERIFIES = "verifies"
    BLOCKS = "blocks"


class TaskStatus(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class PlanPatchKind(StrEnum):
    EXPAND = "expand"
    REVISE = "revise"
    CONTRACT = "contract"


class TaskNode(PlanningContract):
    logical_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    objective: str = Field(min_length=1, max_length=2_000)
    mode: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_.-]*$")
    role_key: str = Field(min_length=1, max_length=120)
    profile_id: str = Field(min_length=1, max_length=200)
    profile_revision: int = Field(ge=1)
    budget_ref: str = Field(min_length=1, max_length=300)
    status: TaskStatus = TaskStatus.PLANNED
    depth: int = Field(ge=0, le=4)
    input_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    success_criteria: tuple[str, ...] = Field(min_length=1, max_length=12)
    stop_criteria: tuple[str, ...] = Field(min_length=1, max_length=12)


class TaskEdge(PlanningContract):
    from_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    to_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    kind: TaskEdgeKind

    @model_validator(mode="after")
    def reject_self_edge(self) -> TaskEdge:
        if self.from_key == self.to_key:
            raise ValueError("task edge cannot point to itself")
        return self


class OrchestrationPlanRevision(PlanningContract):
    root_job_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)
    parent_revision: int | None = Field(default=None, ge=1)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=2_000)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)
    planner_profile_id: str = Field(min_length=1, max_length=200)
    planner_profile_revision: int = Field(ge=1)
    plan_hash: str = Field(default="", pattern=r"^[a-f0-9]{64}$")
    nodes: tuple[TaskNode, ...] = Field(min_length=1, max_length=16)
    edges: tuple[TaskEdge, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_graph(self) -> OrchestrationPlanRevision:
        if self.revision == 1 and self.parent_revision is not None:
            raise ValueError("revision 1 cannot have a parent_revision")
        if self.revision > 1 and self.parent_revision is None:
            raise ValueError("later plan revisions require parent_revision")

        keys = [item.logical_key for item in self.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("task logical_key values must be unique")
        nodes_by_key = {item.logical_key: item for item in self.nodes}
        known = set(keys)
        adjacency: dict[str, set[str]] = {key: set() for key in keys}
        indegree = {key: 0 for key in keys}
        seen_edges: set[tuple[str, str, TaskEdgeKind]] = set()
        for edge in self.edges:
            if edge.from_key not in known or edge.to_key not in known:
                raise ValueError("task edge references an unknown task node")
            identity = (edge.from_key, edge.to_key, edge.kind)
            if identity in seen_edges:
                raise ValueError("task edges must be unique")
            seen_edges.add(identity)
            if edge.to_key not in adjacency[edge.from_key]:
                adjacency[edge.from_key].add(edge.to_key)
                indegree[edge.to_key] += 1

        frontier = [key for key in keys if indegree[key] == 0]
        graph_depth = {key: 0 for key in keys}
        visited = 0
        while frontier:
            current = frontier.pop()
            visited += 1
            for successor in adjacency[current]:
                graph_depth[successor] = max(graph_depth[successor], graph_depth[current] + 1)
                if graph_depth[successor] > 4:
                    raise ValueError("task graph exceeds maximum dependency depth")
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    frontier.append(successor)
        if visited != len(keys):
            raise ValueError("task graph contains a cycle")
        if any(nodes_by_key[key].depth != depth for key, depth in graph_depth.items()):
            raise ValueError("task node depth must match dependency depth")
        expected_hash = canonical_plan_hash(self)
        if self.plan_hash and self.plan_hash != expected_hash:
            raise ValueError("plan_hash does not match canonical plan payload")
        object.__setattr__(self, "plan_hash", expected_hash)
        return self


def canonical_plan_hash(plan: OrchestrationPlanRevision) -> str:
    payload_data = plan.model_dump(mode="json", exclude={"plan_hash"})
    for node in payload_data["nodes"]:
        # Task status is a mutable execution projection; it is not plan identity.
        node.pop("status", None)
    payload_data["nodes"] = sorted(payload_data["nodes"], key=lambda item: item["logical_key"])
    payload_data["edges"] = sorted(
        payload_data["edges"],
        key=lambda item: (item["kind"], item["from_key"], item["to_key"]),
    )
    payload = json.dumps(
        payload_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode()).hexdigest()

class PlanPatchProposal(PlanningContract):
    """A planner's immutable candidate revision, before its CAS-protected commit."""

    root_job_id: str = Field(min_length=1, max_length=200)
    base_revision: int = Field(ge=1)
    base_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: PlanPatchKind
    trigger: str = Field(min_length=1, max_length=2_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    new_plan: OrchestrationPlanRevision
    patch_hash: str = Field(default="", pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_target(self) -> PlanPatchProposal:
        if self.new_plan.root_job_id != self.root_job_id:
            raise ValueError("plan patch root_job_id must match its new plan")
        if self.new_plan.parent_revision != self.base_revision:
            raise ValueError("plan patch new plan must name its base revision as parent")
        if self.new_plan.revision != self.base_revision + 1:
            raise ValueError("plan patch must advance exactly one revision")
        expected_hash = canonical_patch_hash(self)
        if self.patch_hash and self.patch_hash != expected_hash:
            raise ValueError("patch_hash does not match canonical patch payload")
        object.__setattr__(self, "patch_hash", expected_hash)
        return self


class ReplanReceipt(PlanningContract):
    root_job_id: UUID
    parent_attempt_id: UUID
    parent_claim_generation: int = Field(ge=1)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_revision: int = Field(ge=1)
    new_revision: int = Field(ge=2)
    patch_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def canonical_patch_hash(patch: PlanPatchProposal) -> str:
    payload_data = patch.model_dump(mode="json", exclude={"patch_hash"})
    new_plan = payload_data["new_plan"]
    for node in new_plan["nodes"]:
        node.pop("status", None)
    new_plan["nodes"] = sorted(new_plan["nodes"], key=lambda item: item["logical_key"])
    new_plan["edges"] = sorted(
        new_plan["edges"],
        key=lambda item: (item["kind"], item["from_key"], item["to_key"]),
    )
    payload = json.dumps(
        payload_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode()).hexdigest()


# ── V3 ready-set scheduler contracts ─────────────────────────────────────────


class TaskClaimStatus(StrEnum):
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class SchedulerSkipReason(StrEnum):
    NOT_READY = "not_ready"
    CONCURRENCY = "concurrency"
    BUDGET = "budget"
    CAPABILITY = "capability"
    ALREADY_DISPATCHED = "already_dispatched"
    ALREADY_CLAIMED = "already_claimed"
    TERMINAL = "terminal"


class TaskDispatchIntent(PlanningContract):
    """The auditable, deterministic conversion from a frozen TaskNode to an execution spec.

    It is derived only from the frozen claim and the frozen plan task, so a replay
    of the same claim reproduces the same child Job identity.
    """

    root_job_id: UUID
    plan_revision: int = Field(ge=1)
    task_logical_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    claim_generation: int = Field(ge=1)
    graph_step_id: str = Field(min_length=1, max_length=200)
    task_kind: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    role_key: str = Field(min_length=1, max_length=120)
    profile_id: str = Field(min_length=1, max_length=200)
    profile_revision: int = Field(ge=1)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    shard_key: str = Field(min_length=1, max_length=200)
    input_refs: tuple[str, ...] = Field(default=(), max_length=32)
    token_budget: int = Field(gt=0)
    tool_call_budget: int = Field(ge=0)
    deadline: datetime


class PlanTaskClaim(PlanningContract):
    """One durable, append-only claim of a plan task for ready-set dispatch."""

    claim_id: UUID
    root_job_id: UUID
    plan_revision: int = Field(ge=1)
    task_logical_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    claim_generation: int = Field(ge=1)
    lease_owner: str = Field(min_length=1, max_length=200)
    lease_token: UUID
    lease_expires_at: datetime
    status: TaskClaimStatus
    child_job_id: UUID | None = None
    intent: TaskDispatchIntent
    created_at: datetime
    completed_at: datetime | None = None


class SchedulerSkip(PlanningContract):
    task_logical_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    reason: SchedulerSkipReason
    detail: str = ""


class SchedulerTickResult(PlanningContract):
    """One idempotent scan of a claimed root's ready set."""

    root_job_id: UUID
    plan_revision: int = Field(default=0, ge=0)
    dispatched: tuple[PlanTaskClaim, ...] = ()
    skipped: tuple[SchedulerSkip, ...] = ()
    active_count: int = Field(default=0, ge=0)
    ready_remaining: int = Field(default=0, ge=0)
    terminal_failed: int = Field(default=0, ge=0)
    settled_child_count: int = Field(default=0, ge=0)
    auto_retry_count: int = Field(default=0, ge=0)
    complete: bool = False
