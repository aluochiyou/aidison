"""Human-approved regression oracles for payload-free AgentRun trajectories."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from aidison.application.agent_run_trajectory import AgentRunTrajectory
from aidison.domain.models import FrozenModel
from aidison.evaluation.contracts import EvaluationCase, EvaluationLayer, FixtureKind
from aidison.runtime.agent_runs import AgentRunKind, AgentRunStatus


class AgentRunRegressionOracle(FrozenModel):
    """Deterministic trajectory assertions supplied and approved by a human."""

    expected_terminal_status: AgentRunStatus
    required_event_types: tuple[str, ...] = Field(default=(), max_length=32)
    forbidden_failure_codes: tuple[str, ...] = Field(default=(), max_length=32)
    max_provider_attempts: int = Field(ge=0, le=1_000)
    max_consumed_tokens: int = Field(ge=0, le=100_000_000)
    max_ambiguous_effects: int = Field(default=0, ge=0, le=1_000)
    require_self_recovery: bool | None = None

    @model_validator(mode="after")
    def collections_are_unique(self) -> AgentRunRegressionOracle:
        if len(set(self.required_event_types)) != len(self.required_event_types):
            raise ValueError("required_event_types must be unique")
        if len(set(self.forbidden_failure_codes)) != len(self.forbidden_failure_codes):
            raise ValueError("forbidden_failure_codes must be unique")
        return self


class GoldenRegressionTask(FrozenModel):
    """An immutable failed-case fixture promoted only after human review."""

    schema_version: Literal["golden-regression-task.v1"] = "golden-regression-task.v1"
    golden_task_key: str = Field(pattern=r"^golden-[a-f0-9]{32}$")
    project_id: UUID
    source_agent_run_id: UUID
    run_kind: AgentRunKind
    source_candidate_key: str = Field(pattern=r"^failure-[a-f0-9]{32}$")
    source_candidate_ref: str = Field(min_length=1, max_length=500)
    replay_bundle_ref: str = Field(min_length=1, max_length=500)
    replay_bundle_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_outcome: str = Field(min_length=1, max_length=4_000)
    oracle: AgentRunRegressionOracle
    reviewed_by: str = Field(min_length=1, max_length=160)
    reviewed_at: datetime


def golden_regression_evaluation_case(
    *,
    task: GoldenRegressionTask,
    trajectory: AgentRunTrajectory,
) -> EvaluationCase:
    """Bind one approved oracle to a later observed Run without live I/O."""

    if trajectory.project_id != task.project_id:
        raise ValueError("regression trajectory belongs to another project")
    if trajectory.kind is not task.run_kind:
        raise ValueError("regression trajectory kind differs from the Golden Task")
    return EvaluationCase(
        key=task.golden_task_key,
        title="Human-approved AgentRun failure regression",
        description=task.expected_outcome,
        metric_id="agent_run_regression_oracle",
        layer=EvaluationLayer.END_TO_END,
        fixture_kind=FixtureKind.GOLDEN,
        fixture_revision="failure-regression-v1",
        inputs={
            "trajectory": trajectory.model_dump(mode="json"),
            "oracle": task.oracle.model_dump(mode="json"),
            "source_candidate_key": task.source_candidate_key,
        },
    )


__all__ = [
    "AgentRunRegressionOracle",
    "GoldenRegressionTask",
    "golden_regression_evaluation_case",
]
