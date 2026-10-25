"""Contracts for the opt-in, provider-backed controlled-live benchmark."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.evaluation.contracts import CaseStatus, canonical_hash


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ControlledLivePhase(StrEnum):
    PREFLIGHT = "preflight"
    CREATE_PROJECT = "create_project"
    APPROVE_REQUIREMENTS = "approve_requirements"
    DISCOVER_MODULES = "discover_modules"
    APPLY_MODULE_STRUCTURE = "apply_module_structure"
    PLAN_RESEARCH = "plan_research"
    ENQUEUE_RESEARCH = "enqueue_research"
    AWAIT_RUN = "await_run"
    EVALUATE = "evaluate"


class ControlledLiveRequirements(_FrozenModel):
    hard_constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    available_resources: tuple[str, ...] = ()
    usage_context: str = Field(default="", max_length=4_000)
    budget_context: str = Field(default="", max_length=4_000)
    skill_context: str = Field(default="", max_length=4_000)
    unknowns: tuple[str, ...] = ()


class ControlledLiveCase(_FrozenModel):
    """One frozen real-provider scenario and its deterministic acceptance envelope."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    title: str = Field(min_length=1, max_length=200)
    project_name: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=8_000)
    requirements: ControlledLiveRequirements
    planning_brief: str = Field(min_length=1, max_length=2_000)
    objective: str = Field(min_length=1, max_length=2_000)
    research_depth: Literal["focused", "standard", "deep"] = "focused"
    source_strategy: Literal["primary", "independent", "official", "mixed"] = "mixed"
    concept_groups: tuple[tuple[str, ...], ...] = Field(min_length=1)
    min_module_count: int = Field(default=2, ge=1, le=8)
    min_concept_coverage: float = Field(default=0.6, ge=0, le=1)
    min_plan_task_count: int = Field(default=2, ge=1)
    min_supported_modules: int = Field(default=1, ge=1)
    min_evidence_count: int = Field(default=1, ge=1)
    max_concurrency: int = Field(default=2, ge=1, le=4)
    max_token_budget: int = Field(default=40_000, ge=1_000, le=1_000_000_000)
    max_duration_seconds: int = Field(default=900, ge=60, le=7_200)

    @model_validator(mode="after")
    def concept_groups_are_bounded_and_unique(self) -> ControlledLiveCase:
        normalized_groups = []
        for group in self.concept_groups:
            normalized = tuple(item.strip().casefold() for item in group)
            if not normalized or any(not item for item in normalized):
                raise ValueError("controlled-live concept groups must not be empty")
            if len(set(normalized)) != len(normalized):
                raise ValueError("controlled-live concept alternatives must be unique")
            normalized_groups.append(normalized)
        if len(set(normalized_groups)) != len(normalized_groups):
            raise ValueError("controlled-live concept groups must be unique")
        return self

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class ControlledLiveManifest(_FrozenModel):
    suite_key: str = "aidison-controlled-live"
    suite_revision: str = "v1"
    case_hashes: tuple[tuple[str, str], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def hashes_are_unique_and_valid(self) -> ControlledLiveManifest:
        keys = [key for key, _ in self.case_hashes]
        if len(keys) != len(set(keys)):
            raise ValueError("controlled-live manifest contains duplicate case keys")
        for key, digest in self.case_hashes:
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"controlled-live hash for {key!r} must be sha256")
        return self

    @classmethod
    def from_cases(cls, cases: tuple[ControlledLiveCase, ...]) -> ControlledLiveManifest:
        return cls(case_hashes=tuple(sorted((case.key, case.content_hash) for case in cases)))

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.suite_key, self.suite_revision, self.case_hashes)


class ControlledLiveCaseResult(_FrozenModel):
    case_key: str
    title: str
    status: CaseStatus
    phase: ControlledLivePhase
    project_id: UUID | None = None
    agent_run_id: UUID | None = None
    project_revision: int | None = Field(default=None, ge=1)
    module_count: int = Field(default=0, ge=0)
    concept_groups_matched: int = Field(default=0, ge=0)
    concept_group_count: int = Field(default=0, ge=0)
    concept_coverage: float = Field(default=0, ge=0, le=1)
    plan_task_count: int = Field(default=0, ge=0)
    final_run_status: str | None = None
    pending_decision_count: int = Field(default=0, ge=0)
    supported_module_count: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    token_consumed: int = Field(default=0, ge=0)
    tool_calls_consumed: int = Field(default=0, ge=0)
    failures: tuple[str, ...] = ()
    request_trace_ids: tuple[str, ...] = ()
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    error: str | None = None

    @model_validator(mode="after")
    def status_payload_is_consistent(self) -> ControlledLiveCaseResult:
        if self.status is CaseStatus.ERRORED and not self.error:
            raise ValueError("errored controlled-live case requires an error")
        if self.status is CaseStatus.FAILED and not self.failures:
            raise ValueError("failed controlled-live case requires deterministic failures")
        return self


class ControlledLiveSummary(_FrozenModel):
    case_count: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errored: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    token_consumed: int = Field(ge=0)
    tool_calls_consumed: int = Field(ge=0)

    @property
    def all_passed(self) -> bool:
        return self.case_count > 0 and self.failed == 0 and self.errored == 0


class ControlledLiveReport(_FrozenModel):
    schema_version: str = "aidison-controlled-live-report/v1"
    suite_run_id: UUID
    base_url: str
    manifest: ControlledLiveManifest
    started_at: datetime
    completed_at: datetime
    summary: ControlledLiveSummary
    cases: tuple[ControlledLiveCaseResult, ...]

    @model_validator(mode="after")
    def summary_matches_cases(self) -> ControlledLiveReport:
        if self.summary.case_count != len(self.cases):
            raise ValueError("controlled-live summary does not match case count")
        return self
