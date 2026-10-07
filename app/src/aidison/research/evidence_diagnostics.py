"""Typed, non-admitted diagnostics for Research evidence materialization.

These records explain why a model-proposed claim could not enter the shared
evidence projection.  They are deliberately Artifact-only: diagnostics never
count as evidence, satisfy Coverage, or unlock a dependent task.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ResearchEvidenceMaterializationIssue(BaseModel):
    """One rejected claim, without retaining model scratchpad or source body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_index: int = Field(ge=0)
    coverage_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    source_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    assigned_coverage_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)


class ResearchEvidenceMaterializationReport(BaseModel):
    """Immutable diagnostic projection for one ResultEnvelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "research-evidence-materialization-report-v1"
    result_id: UUID
    task_id: UUID
    issues: tuple[ResearchEvidenceMaterializationIssue, ...] = Field(
        min_length=1, max_length=64
    )


class ResearchSourceCollectionReport(BaseModel):
    """Immutable audit summary of the trusted documents shown to one task.

    This is deliberately not evidence admission.  It records the frozen
    collection input so a later Coverage gap can distinguish a weak source
    set from an invalid model citation without exposing source bodies again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "research-source-collection-report-v1"
    result_id: UUID
    task_id: UUID
    coverage_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    collection_profile: str = Field(pattern=r"^(focused|standard|deep)$")
    configured_max_queries: int | None = Field(default=None, ge=1)
    configured_max_documents: int | None = Field(default=None, ge=1)
    collected_source_count: int = Field(ge=1)
    collected_source_kinds: tuple[str, ...] = Field(min_length=1, max_length=16)
    source_snapshot_refs: tuple[str, ...] = Field(min_length=1)
    unavailable_coverage_reasons: tuple[ResearchSourceCollectionFailure, ...] = Field(
        default=(), max_length=16
    )


class ResearchSourceCollectionFailure(BaseModel):
    """One bounded subquery failure attached to an otherwise usable source set.

    This is collection telemetry, not evidence and not a claim rejection.  It
    explains why a concrete Coverage Key may remain unresolved even though a
    sibling query in the same task yielded a saved source snapshot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")


class ResearchSourceUnavailableReport(BaseModel):
    """A bounded source-gap diagnostic for an admitted partial task result.

    The report is not Evidence and cannot answer a Coverage Key.  It exists so
    a temporary source failure is visible to the gap planner and the user
    without inventing a model answer or discarding independent task results.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "research-source-unavailable-report-v1"
    result_id: UUID
    task_id: UUID
    coverage_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")


__all__ = [
    "ResearchEvidenceMaterializationIssue",
    "ResearchEvidenceMaterializationReport",
    "ResearchSourceCollectionFailure",
    "ResearchSourceCollectionReport",
    "ResearchSourceUnavailableReport",
]
