"""Read-only, secret-free trajectory exports and matched-budget comparisons.

This module deliberately consumes compact observation DTOs rather than ORM rows.
Production adapters may construct those DTOs later, while the evaluation core
remains deterministic and cannot mutate an AgentRun or Project.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator

from aidison.evaluation.contracts import FrozenModel, canonical_hash
from aidison.runtime.agent_run_budget import AgentRunBudgetOperationKind


class TrajectoryEventKind(StrEnum):
    TASK_PLANNED = "task_planned"
    TASK_UNLOCKED = "task_unlocked"
    RESULT_ADMITTED = "result_admitted"
    COVERAGE_CHANGED = "coverage_changed"
    CONFLICT_CHANGED = "conflict_changed"
    PROPOSAL_READY = "proposal_ready"
    DECISION_REQUEST_CREATED = "decision_request_created"
    EFFECT_RECONCILED = "effect_reconciled"
    FAILURE_RECORDED = "failure_recorded"


class TrajectoryBinding(FrozenModel):
    """Pinned configuration and basis required to interpret one AgentRun trace."""

    run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    graph_revision: str = Field(min_length=1, max_length=200)
    state_schema_version: str = Field(min_length=1, max_length=100)
    profile_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class TrajectoryInvocation(FrozenModel):
    """One physical, externally observable model or tool attempt.

    It has no prompt, response body, credentials, hidden reasoning or provider
    payload.  Such data remains outside the evaluation trajectory.
    """

    invocation_id: UUID
    occurred_at: datetime
    module_key: str | None = Field(default=None, min_length=1, max_length=200)
    task_key: str = Field(min_length=1, max_length=300)
    kind: AgentRunBudgetOperationKind
    provider: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    physical_attempt_no: int = Field(ge=1, le=100)
    token_usage: int = Field(default=0, ge=0)
    tool_call_usage: int = Field(default=0, ge=0)
    latency_ms: int = Field(ge=0)
    result_admitted: bool = False
    fallback_used: bool = False
    failure_code: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def usage_matches_operation_kind(self) -> TrajectoryInvocation:
        if self.kind is AgentRunBudgetOperationKind.MODEL and self.tool_call_usage != 0:
            raise ValueError("model trajectory invocation cannot consume tool calls")
        if self.kind is AgentRunBudgetOperationKind.TOOL and self.token_usage != 0:
            raise ValueError("tool trajectory invocation cannot consume model tokens")
        return self


class TrajectoryEvent(FrozenModel):
    """One durable, auditable state transition without event body text."""

    occurred_at: datetime
    kind: TrajectoryEventKind
    run_id: UUID | None = None
    task_key: str | None = Field(default=None, min_length=1, max_length=300)
    ref_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CostAttribution(FrozenModel):
    """Aggregated physical costs at the Run → Module → Task → invocation target path."""

    module_key: str | None
    task_key: str
    kind: AgentRunBudgetOperationKind
    provider: str
    target: str
    invocation_count: int = Field(ge=1)
    retry_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
    token_usage: int = Field(ge=0)
    tool_call_usage: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    admitted_result_count: int = Field(ge=0)
    no_progress_tokens: int = Field(ge=0)
    no_progress_tool_calls: int = Field(ge=0)


class TrajectorySummary(FrozenModel):
    total_invocations: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    total_tool_calls: int = Field(ge=0)
    total_latency_ms: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
    admitted_result_count: int = Field(ge=0)
    meaningful_event_count: int = Field(ge=0)
    cost_without_admitted_result_tokens: int = Field(ge=0)
    cost_without_admitted_result_tool_calls: int = Field(ge=0)


class TrajectoryExport(FrozenModel):
    binding: TrajectoryBinding
    summary: TrajectorySummary
    attribution: tuple[CostAttribution, ...]
    event_kinds: tuple[TrajectoryEventKind, ...]
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def build_trajectory_export(
    *,
    binding: TrajectoryBinding,
    invocations: tuple[TrajectoryInvocation, ...],
    events: tuple[TrajectoryEvent, ...],
) -> TrajectoryExport:
    """Create an immutable evaluation projection from observations of one run."""
    if len({item.invocation_id for item in invocations}) != len(invocations):
        raise ValueError("trajectory must not contain duplicate invocation ids")
    foreign_events = [event for event in events if event.run_id not in (None, binding.run_id)]
    if foreign_events:
        raise ValueError("trajectory events must belong to the same AgentRun as the binding")
    ordered_invocations = tuple(
        sorted(invocations, key=lambda item: (item.occurred_at, str(item.invocation_id)))
    )
    ordered_events = tuple(sorted(events, key=lambda item: (item.occurred_at, item.kind.value)))
    attribution = _attribute(ordered_invocations)
    summary = TrajectorySummary(
        total_invocations=len(ordered_invocations),
        total_tokens=sum(item.token_usage for item in ordered_invocations),
        total_tool_calls=sum(item.tool_call_usage for item in ordered_invocations),
        total_latency_ms=sum(item.latency_ms for item in ordered_invocations),
        retry_count=sum(item.physical_attempt_no > 1 for item in ordered_invocations),
        fallback_count=sum(item.fallback_used for item in ordered_invocations),
        admitted_result_count=sum(item.result_admitted for item in ordered_invocations),
        meaningful_event_count=sum(
            event.kind
            in {
                TrajectoryEventKind.RESULT_ADMITTED,
                TrajectoryEventKind.COVERAGE_CHANGED,
                TrajectoryEventKind.CONFLICT_CHANGED,
                TrajectoryEventKind.TASK_UNLOCKED,
                TrajectoryEventKind.DECISION_REQUEST_CREATED,
                TrajectoryEventKind.EFFECT_RECONCILED,
            }
            for event in ordered_events
        ),
        cost_without_admitted_result_tokens=sum(
            item.token_usage for item in ordered_invocations if not item.result_admitted
        ),
        cost_without_admitted_result_tool_calls=sum(
            item.tool_call_usage for item in ordered_invocations if not item.result_admitted
        ),
    )
    digest = canonical_hash(binding, ordered_invocations, ordered_events, attribution, summary)
    return TrajectoryExport(
        binding=binding,
        summary=summary,
        attribution=attribution,
        event_kinds=tuple(sorted({event.kind for event in ordered_events}, key=str)),
        content_hash=digest,
    )


def _attribute(invocations: tuple[TrajectoryInvocation, ...]) -> tuple[CostAttribution, ...]:
    grouped: dict[
        tuple[str | None, str, AgentRunBudgetOperationKind, str, str], list[TrajectoryInvocation]
    ] = defaultdict(list)
    for item in invocations:
        grouped[(item.module_key, item.task_key, item.kind, item.provider, item.target)].append(
            item
        )
    rows = []
    for key, items in grouped.items():
        module_key, task_key, kind, provider, target = key
        rows.append(
            CostAttribution(
                module_key=module_key,
                task_key=task_key,
                kind=kind,
                provider=provider,
                target=target,
                invocation_count=len(items),
                retry_count=sum(item.physical_attempt_no > 1 for item in items),
                fallback_count=sum(item.fallback_used for item in items),
                token_usage=sum(item.token_usage for item in items),
                tool_call_usage=sum(item.tool_call_usage for item in items),
                latency_ms=sum(item.latency_ms for item in items),
                admitted_result_count=sum(item.result_admitted for item in items),
                no_progress_tokens=sum(
                    item.token_usage for item in items if not item.result_admitted
                ),
                no_progress_tool_calls=sum(
                    item.tool_call_usage for item in items if not item.result_admitted
                ),
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                item.module_key or "",
                item.task_key,
                item.kind.value,
                item.provider,
                item.target,
            ),
        )
    )


class AblationBudget(FrozenModel):
    """All frozen conditions that must match before comparing orchestration arms."""

    fixture_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    token_cap: int = Field(ge=1)
    tool_call_cap: int = Field(ge=0)
    max_latency_ms: int = Field(ge=1)
    concurrency_cap: int = Field(ge=1)

    @property
    def content_hash(self) -> str:
        return canonical_hash(
            self.fixture_manifest_hash,
            self.model_binding_hash,
            self.tool_policy_hash,
            self.token_cap,
            self.tool_call_cap,
            self.max_latency_ms,
            self.concurrency_cap,
        )


class AblationQuality(FrozenModel):
    coverage_completion: float = Field(ge=0, le=1)
    unsupported_claim_rate: float = Field(ge=0, le=1)
    conflict_recall: float = Field(ge=0, le=1)


class AblationArm(FrozenModel):
    arm_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    run_id: UUID
    budget: AblationBudget
    quality: AblationQuality
    observed_tokens: int = Field(ge=0)
    observed_tool_calls: int = Field(ge=0)
    observed_latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def observations_stay_inside_the_frozen_budget(self) -> AblationArm:
        if self.observed_tokens > self.budget.token_cap:
            raise ValueError("observed tokens exceed frozen token cap")
        if self.observed_tool_calls > self.budget.tool_call_cap:
            raise ValueError("observed tool calls exceed frozen tool-call cap")
        if self.observed_latency_ms > self.budget.max_latency_ms:
            raise ValueError("observed latency exceeds frozen latency cap")
        return self


class IsoBudgetComparison(FrozenModel):
    budget: AblationBudget
    arms: tuple[AblationArm, ...] = Field(min_length=2)
    is_iso_budget: bool = True
    pareto_arm_keys: tuple[str, ...] = Field(min_length=1)


def compare_iso_budget(arms: tuple[AblationArm, ...]) -> IsoBudgetComparison:
    """Reject unfair experiments and compute a transparent quality-cost-time frontier."""
    if len(arms) < 2:
        raise ValueError("iso-budget comparison requires at least two arms")
    if len({arm.arm_key for arm in arms}) != len(arms):
        raise ValueError("iso-budget comparison requires unique arm keys")
    reference = arms[0].budget
    if any(arm.budget.content_hash != reference.content_hash for arm in arms[1:]):
        raise ValueError("all ablation arms must use the same frozen budget")
    ordered = tuple(sorted(arms, key=lambda item: item.arm_key))
    pareto = tuple(
        arm.arm_key
        for arm in ordered
        if not any(_dominates(other, arm) for other in ordered if other.arm_key != arm.arm_key)
    )
    return IsoBudgetComparison(budget=reference, arms=ordered, pareto_arm_keys=pareto)


def _dominates(left: AblationArm, right: AblationArm) -> bool:
    not_worse = (
        left.quality.coverage_completion >= right.quality.coverage_completion
        and left.quality.unsupported_claim_rate <= right.quality.unsupported_claim_rate
        and left.quality.conflict_recall >= right.quality.conflict_recall
        and left.observed_tokens <= right.observed_tokens
        and left.observed_tool_calls <= right.observed_tool_calls
        and left.observed_latency_ms <= right.observed_latency_ms
    )
    strictly_better = (
        left.quality.coverage_completion > right.quality.coverage_completion
        or left.quality.unsupported_claim_rate < right.quality.unsupported_claim_rate
        or left.quality.conflict_recall > right.quality.conflict_recall
        or left.observed_tokens < right.observed_tokens
        or left.observed_tool_calls < right.observed_tool_calls
        or left.observed_latency_ms < right.observed_latency_ms
    )
    return not_worse and strictly_better


__all__ = [
    "AblationArm",
    "AblationBudget",
    "AblationQuality",
    "CostAttribution",
    "IsoBudgetComparison",
    "TrajectoryBinding",
    "TrajectoryEvent",
    "TrajectoryEventKind",
    "TrajectoryExport",
    "TrajectoryInvocation",
    "TrajectorySummary",
    "build_trajectory_export",
    "compare_iso_budget",
]
