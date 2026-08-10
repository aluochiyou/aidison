"""Evaluation domain contracts: cases, runs, per-case results, metrics and summaries.

The evaluation layer is deliberately small and independent of the canonical
domain: a deterministic, offline regression harness that scores runtime
invariants and structured output contracts, plus a JSON-safe, auditable report.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum, StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

_NAMESPACE = UUID("6f2b9a10-4d7e-4c0a-9f4e-8c1d2a3b4c5d")
_SCHEMA_VERSION = "aidison-evaluation/v1"
_RUNNER_VERSION = "0.1.0"


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvaluationMode(StrEnum):
    """Execution mode of one evaluation run."""

    OFFLINE = "offline"
    LIVE = "live"


class MetricStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


class CaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    SKIPPED = "skipped"


class MetricResult(FrozenModel):
    """One deterministic check outcome; payload is always JSON-safe."""

    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    status: MetricStatus
    detail: str = Field(default="", max_length=8_000)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status is MetricStatus.PASS


class EvaluationCase(FrozenModel):
    """A fixed, auditable scenario with JSON-safe inputs.

    ``inputs`` is the exact data the checker consumed, so a stored report fully
    reconstructs what was evaluated without dereferencing any external source.
    """

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4_000)
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    inputs: dict[str, Any] = Field(default_factory=dict)


class CaseResult(FrozenModel):
    case_key: str
    title: str
    metric_id: str
    status: CaseStatus
    metrics: tuple[MetricResult, ...] = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    error: str | None = None

    @model_validator(mode="after")
    def errored_case_carries_message(self) -> CaseResult:
        if self.status is CaseStatus.ERRORED and not self.error:
            raise ValueError("errored case result requires an error message")
        return self


class EvaluationSummary(FrozenModel):
    case_count: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errored: int = Field(ge=0)
    skipped: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)

    @property
    def all_passed(self) -> bool:
        return self.case_count > 0 and self.failed == 0 and self.errored == 0


class EvaluationReport(FrozenModel):
    """Full, JSON-safe evaluation report. Never contains secrets or config keys."""

    schema_version: str = _SCHEMA_VERSION
    runner_version: str = _RUNNER_VERSION
    run_id: UUID
    mode: EvaluationMode
    reporter: str = "none"
    generated_at: datetime
    summary: EvaluationSummary
    cases: tuple[CaseResult, ...]

    @model_validator(mode="after")
    def summary_matches_cases(self) -> EvaluationReport:
        if len(self.cases) != self.summary.case_count:
            raise ValueError("summary case_count must equal the number of case results")
        return self


def canonical_hash(*values: object) -> str:
    """Small canonical hash shared by the evaluation layer only.

    It mirrors ``aidison.application.service.canonical_hash`` without importing
    the full canonical domain, keeping the evaluation layer independently runnable.
    """

    def default(value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, datetime):
            normalized = value.astimezone(UTC) if value.tzinfo is not None else value
            return normalized.isoformat().replace("+00:00", "Z")
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"unsupported value in canonical payload: {type(value).__name__}")

    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=default)
    return sha256(payload.encode()).hexdigest()


def derive_run_id(*, mode: EvaluationMode, case_keys: tuple[str, ...]) -> UUID:
    """Deterministic run identity: identical inputs always derive the same run id."""
    payload = canonical_hash(_SCHEMA_VERSION, _RUNNER_VERSION, mode, tuple(sorted(case_keys)))
    return uuid5(_NAMESPACE, payload)
