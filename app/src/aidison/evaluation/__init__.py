"""Deterministic, auditable evaluation of Aidison agent and runtime behavior.

Public entry points:

- ``run_evaluation`` — run fixed fixture cases and return a JSON-safe report.
- ``FIXTURE_CASES`` — the offline regression baseline.
- ``EvaluationSettings`` / ``build_evaluation_reporter`` — optional Langfuse wiring.
"""

from __future__ import annotations

from aidison.evaluation.contracts import (
    CaseResult,
    CaseStatus,
    EvaluationCase,
    EvaluationLayer,
    EvaluationMode,
    EvaluationReport,
    EvaluationSummary,
    FixtureKind,
    FrozenFixtureManifest,
    MetricResult,
    MetricStatus,
)
from aidison.evaluation.fixtures import FIXTURE_CASES
from aidison.evaluation.langfuse_reporter import LangfuseEvaluationReporter
from aidison.evaluation.langsmith_reporter import (
    LangSmithEvaluationReporter,
)
from aidison.evaluation.reporter import EvaluationReporter, NoopEvaluationReporter
from aidison.evaluation.runner import EvaluationError, EvaluationNoCasesError, run_evaluation
from aidison.evaluation.settings import EvaluationSettings, build_evaluation_reporter
from aidison.evaluation.trajectory import (
    AblationArm,
    AblationBudget,
    AblationQuality,
    CostAttribution,
    IsoBudgetComparison,
    TrajectoryBinding,
    TrajectoryEvent,
    TrajectoryEventKind,
    TrajectoryExport,
    TrajectoryInvocation,
    TrajectorySummary,
    build_trajectory_export,
    compare_iso_budget,
)

__all__ = [
    "CaseResult",
    "CaseStatus",
    "CostAttribution",
    "EvaluationCase",
    "EvaluationError",
    "EvaluationLayer",
    "EvaluationMode",
    "EvaluationNoCasesError",
    "EvaluationReport",
    "EvaluationReporter",
    "EvaluationSettings",
    "EvaluationSummary",
    "FixtureKind",
    "FIXTURE_CASES",
    "FrozenFixtureManifest",
    "IsoBudgetComparison",
    "LangSmithEvaluationReporter",
    "LangfuseEvaluationReporter",
    "MetricResult",
    "MetricStatus",
    "NoopEvaluationReporter",
    "build_evaluation_reporter",
    "build_trajectory_export",
    "compare_iso_budget",
    "run_evaluation",
    "AblationArm",
    "AblationBudget",
    "AblationQuality",
    "TrajectoryBinding",
    "TrajectoryEvent",
    "TrajectoryEventKind",
    "TrajectoryExport",
    "TrajectoryInvocation",
    "TrajectorySummary",
]
