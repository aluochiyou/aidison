"""Vendor-neutral reporting boundary for completed evaluation reports."""

from __future__ import annotations

from typing import Protocol

from aidison.evaluation.contracts import EvaluationReport


class EvaluationReporter(Protocol):
    """Optional export boundary; it can never own the local evaluation result."""

    name: str

    @property
    def enabled(self) -> bool: ...

    def report(self, report: EvaluationReport) -> None: ...


class NoopEvaluationReporter:
    """Disabled sink used when no exporter is configured."""

    name = "none"

    @property
    def enabled(self) -> bool:
        return False

    def report(self, report: EvaluationReport) -> None:
        del report


__all__ = ["EvaluationReporter", "NoopEvaluationReporter"]
