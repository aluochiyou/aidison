"""Deterministic, auditable evaluation of Aidison agent and runtime behavior.

Public entry points:

- ``run_evaluation`` — run fixed fixture cases and return a JSON-safe report.
- ``FIXTURE_CASES`` — the offline regression baseline.
- ``EvaluationSettings`` / ``build_evaluation_reporter`` — optional LangSmith wiring.
"""

from __future__ import annotations

from aidison.evaluation.contracts import (
    CaseResult,
    CaseStatus,
    EvaluationCase,
    EvaluationMode,
    EvaluationReport,
    EvaluationSummary,
    MetricResult,
    MetricStatus,
)
from aidison.evaluation.fixtures import FIXTURE_CASES
from aidison.evaluation.langsmith_reporter import (
    EvaluationReporter,
    LangSmithEvaluationReporter,
    NoopEvaluationReporter,
)
from aidison.evaluation.runner import EvaluationError, EvaluationNoCasesError, run_evaluation
from aidison.evaluation.settings import EvaluationSettings, build_evaluation_reporter

__all__ = [
    "CaseResult",
    "CaseStatus",
    "EvaluationCase",
    "EvaluationError",
    "EvaluationMode",
    "EvaluationNoCasesError",
    "EvaluationReport",
    "EvaluationReporter",
    "EvaluationSettings",
    "EvaluationSummary",
    "FIXTURE_CASES",
    "LangSmithEvaluationReporter",
    "MetricResult",
    "MetricStatus",
    "NoopEvaluationReporter",
    "build_evaluation_reporter",
    "run_evaluation",
]
