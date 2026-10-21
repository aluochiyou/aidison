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


class EvaluationLayer(StrEnum):
    """The independent question an evaluation case is designed to answer."""

    CONTRACT = "contract"
    CAPABILITY = "capability"
    TRAJECTORY = "trajectory"
    END_TO_END = "end_to_end"
    ROBUSTNESS_SECURITY = "robustness_security"
    COST_LATENCY = "cost_latency"


class FixtureKind(StrEnum):
    """How the frozen inputs were curated; not an execution mode."""

    GOLDEN = "golden"
    MUTATION = "mutation"
    FAULT = "fault"
    TRACE_REVIEW = "trace_review"
    CONTROLLED_LIVE = "controlled_live"


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
    layer: EvaluationLayer = EvaluationLayer.CONTRACT
    fixture_kind: FixtureKind = FixtureKind.GOLDEN
    fixture_revision: str = Field(default="v1", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    inputs: dict[str, Any] = Field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Stable identity of every input and scoring contract for this case."""
        return canonical_hash(
            self.key,
            self.title,
            self.description,
            self.metric_id,
            self.layer,
            self.fixture_kind,
            self.fixture_revision,
            self.inputs,
        )


class FrozenFixtureManifest(FrozenModel):
    """Content-addressed evidence of the exact cases that an EvalReport ran."""

    fixture_set_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    fixture_set_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    case_hashes: tuple[tuple[str, str], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_hashes_are_unique_and_well_formed(self) -> FrozenFixtureManifest:
        keys = [key for key, _ in self.case_hashes]
        if len(keys) != len(set(keys)):
            raise ValueError("fixture manifest contains duplicate case keys")
        for key, digest in self.case_hashes:
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"fixture case hash for {key!r} must be a lowercase sha256 digest")
        return self

    @classmethod
    def from_cases(
        cls,
        *,
        fixture_set_key: str,
        fixture_set_revision: str,
        cases: tuple[EvaluationCase, ...],
    ) -> FrozenFixtureManifest:
        return cls(
            fixture_set_key=fixture_set_key,
            fixture_set_revision=fixture_set_revision,
            case_hashes=tuple(sorted((case.key, case.content_hash) for case in cases)),
        )

    @property
    def case_keys(self) -> tuple[str, ...]:
        return tuple(key for key, _ in sorted(self.case_hashes))

    @property
    def content_hash(self) -> str:
        return canonical_hash(
            self.fixture_set_key,
            self.fixture_set_revision,
            tuple(sorted(self.case_hashes)),
        )


class CaseResult(FrozenModel):
    case_key: str
    title: str
    metric_id: str
    layer: EvaluationLayer = EvaluationLayer.CONTRACT
    fixture_kind: FixtureKind = FixtureKind.GOLDEN
    fixture_revision: str = "v1"
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
    fixture_manifest: FrozenFixtureManifest | None = None
    layers_covered: tuple[EvaluationLayer, ...] = ()

    @model_validator(mode="after")
    def summary_matches_cases(self) -> EvaluationReport:
        if len(self.cases) != self.summary.case_count:
            raise ValueError("summary case_count must equal the number of case results")
        if self.fixture_manifest is not None:
            result_keys = tuple(sorted(case.case_key for case in self.cases))
            if result_keys != self.fixture_manifest.case_keys:
                raise ValueError("fixture manifest case keys must equal report case keys")
        actual_layers = tuple(sorted({case.layer for case in self.cases}, key=str))
        if self.layers_covered and self.layers_covered != actual_layers:
            raise ValueError("layers_covered must equal layers represented by report cases")
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


def derive_run_id(
    *,
    mode: EvaluationMode,
    case_keys: tuple[str, ...],
    fixture_manifest_hash: str | None = None,
) -> UUID:
    """Deterministic run identity: identical inputs always derive the same run id."""
    payload = canonical_hash(
        _SCHEMA_VERSION,
        _RUNNER_VERSION,
        mode,
        tuple(sorted(case_keys)),
        fixture_manifest_hash,
    )
    return uuid5(_NAMESPACE, payload)
