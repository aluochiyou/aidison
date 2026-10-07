"""Frozen, server-owned contract for one approved Research Run.

The user approves an :class:`ExecutionPlanProposal`; this compact artifact
freezes only the execution-relevant projection before the Run starts.  It
prevents a later plan edit or a changed active-module list from silently
changing an already authorized research graph.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from aidison.domain.models import FrozenModel, ResearchStrategyProposal
from aidison.research.adaptive_planning import AdaptivePlanningPolicy


class ResearchCollectionPolicy(FrozenModel):
    """Provider collection policy frozen with the approved Run.

    ``None`` means the product does not impose a breadth cap.  Individual HTTP
    requests, provider quotas, source normalization and the Run duration still
    remain bounded independently.
    """

    profile: str = Field(pattern=r"^(focused|standard|deep)$")
    max_queries: int | None = Field(default=None, ge=1)
    max_documents_total: int | None = Field(default=None, ge=1)
    max_documents_per_query: int | None = Field(default=None, ge=1)
    search_depth: str = Field(pattern=r"^(basic|advanced)$")

    @model_validator(mode="after")
    def per_query_limit_cannot_exceed_total(self) -> ResearchCollectionPolicy:
        if (
            self.max_documents_per_query is not None
            and self.max_documents_total is not None
            and self.max_documents_per_query > self.max_documents_total
        ):
            raise ValueError("per-query source limit cannot exceed the task source limit")
        return self


class ResearchExecutionPolicy(FrozenModel):
    """Frozen operational limits for one Research Run."""

    # v2 adds origin diversity for deep MUST coverage. A run freezes this
    # policy, so a v1 contract must never silently acquire stricter semantics
    # after it has been approved.
    policy_version: str = "research-execution-policy-v2"
    collection: ResearchCollectionPolicy
    adaptive: AdaptivePlanningPolicy
    minimum_evidence_sources_for_must: int = Field(default=1, ge=1, le=5)
    minimum_evidence_origins_for_must: int = Field(default=1, ge=1, le=5)


def compile_research_execution_policy(
    *,
    research_depth: str,
) -> ResearchExecutionPolicy:
    """Map user-approved depth to a frozen collection and expansion policy."""

    policies = {
        "focused": ResearchExecutionPolicy(
            collection=ResearchCollectionPolicy(
                profile="focused",
                max_queries=2,
                max_documents_total=2,
                max_documents_per_query=1,
                search_depth="basic",
            ),
            adaptive=AdaptivePlanningPolicy(
                max_patch_revisions=1,
                max_total_tasks=8,
                max_tasks_per_patch=4,
                max_consecutive_no_progress=1,
            ),
            minimum_evidence_sources_for_must=1,
            minimum_evidence_origins_for_must=1,
        ),
        "standard": ResearchExecutionPolicy(
            collection=ResearchCollectionPolicy(
                profile="standard",
                max_queries=3,
                max_documents_total=3,
                max_documents_per_query=1,
                search_depth="basic",
            ),
            adaptive=AdaptivePlanningPolicy(),
            minimum_evidence_sources_for_must=1,
            minimum_evidence_origins_for_must=1,
        ),
        "deep": ResearchExecutionPolicy(
            collection=ResearchCollectionPolicy(
                profile="deep",
                max_queries=None,
                max_documents_total=None,
                max_documents_per_query=None,
                search_depth="advanced",
            ),
            adaptive=AdaptivePlanningPolicy(
                max_patch_revisions=None,
                max_total_tasks=None,
                max_tasks_per_patch=None,
                max_consecutive_no_progress=None,
            ),
            minimum_evidence_sources_for_must=2,
            minimum_evidence_origins_for_must=2,
        ),
    }
    try:
        return policies[research_depth]
    except KeyError as exc:
        raise ValueError("unknown research depth") from exc


class ResearchRunContract(FrozenModel):
    """Immutable authorization projection consumed by ``ResearchRunExecutor``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "research-run-contract-v1"
    agent_run_id: UUID
    execution_plan_id: UUID
    execution_plan_scope_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_module_ids: tuple[UUID, ...] = Field(min_length=1, max_length=64)
    max_concurrency: int = Field(ge=1, le=16)
    max_token_budget: int = Field(gt=0)
    max_duration_seconds: int = Field(default=36_000, ge=60, le=604_800)
    requires_independent_verification: bool = False
    execution_policy: ResearchExecutionPolicy = Field(
        default_factory=lambda: compile_research_execution_policy(research_depth="standard")
    )
    research_strategy: ResearchStrategyProposal | None = None

    @model_validator(mode="after")
    def strategy_scope_matches_frozen_scope(self) -> ResearchRunContract:
        if len(set(self.scope_module_ids)) != len(self.scope_module_ids):
            raise ValueError("research run contract scope_module_ids must be unique")
        if self.research_strategy is not None and set(
            self.research_strategy.scope_module_ids
        ) != set(self.scope_module_ids):
            raise ValueError("research strategy scope does not match the frozen run scope")
        return self


__all__ = [
    "ResearchCollectionPolicy",
    "ResearchExecutionPolicy",
    "ResearchRunContract",
    "compile_research_execution_policy",
]
