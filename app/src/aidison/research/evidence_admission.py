"""Deterministic Evidence Candidate admission before any Domain write.

Models and extractors may propose a relation between a claim and a cited
``SourceSpan``.  This module validates the complete source lineage, Artifact
identity/status, policy scope, freshness, and exact quote location before that
proposal can be mapped to the existing ``EvidenceBinding`` Domain object.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.artifacts.contracts import ArtifactMetadata, ArtifactStatus
from aidison.domain.models import EvidenceBinding, EvidenceStatus
from aidison.research.source_observations import (
    ObservationOutcome,
    SourceIdentity,
    SourceKind,
    SourceObservation,
    SourceSnapshot,
    SourceSpan,
)


class EvidenceRelation(StrEnum):
    """An extractor's proposed relation, deliberately not an Evidence status."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class EvidenceAdmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EvidenceLifecycle(StrEnum):
    """Usability lifecycle, intentionally independent from supports/contradicts."""

    ACTIVE = "active"
    STALE = "stale"
    RETRACTED = "retracted"
    SUPERSEDED = "superseded"
    QUARANTINED = "quarantined"


class EvidenceInvalidationTarget(StrEnum):
    """Projections that must reconsider an accepted Binding after invalidation."""

    COVERAGE = "coverage"
    VERIFICATION = "verification"
    IMPACT = "impact"


class EvidenceCandidate(BaseModel):
    """Untrusted claim proposal bound to a complete, typed source lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    module_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: SourceIdentity
    observation: SourceObservation
    snapshot: SourceSnapshot
    span: SourceSpan
    relation: EvidenceRelation
    claim: str = Field(min_length=1, max_length=8_000)
    applicability: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def source_lineage_is_complete_and_non_ambiguous(self) -> EvidenceCandidate:
        source_id = self.source.id
        assert source_id is not None
        if self.snapshot.source_id != source_id:
            raise ValueError("snapshot does not belong to the candidate source identity")
        if self.observation.basis_hash != self.basis_hash:
            raise ValueError("observation basis does not match candidate basis")
        if self.observation.resolved_source_id != source_id:
            raise ValueError("observation did not resolve to the candidate source identity")
        if self.observation.outcome is ObservationOutcome.FAILED:
            raise ValueError("failed observation cannot supply an evidence candidate")
        if self.observation.snapshot_id != self.snapshot.id:
            raise ValueError("observation does not reference the candidate snapshot")
        if self.span.snapshot_id != self.snapshot.id:
            raise ValueError("span does not reference the candidate snapshot")
        if self.span.artifact_ref != self.snapshot.artifact_ref:
            raise ValueError("span artifact_ref does not match candidate snapshot")
        if self.span.content_hash != self.snapshot.content_hash:
            raise ValueError("span content_hash does not match candidate snapshot")
        return self


class EvidenceAdmissionPolicy(BaseModel):
    """Frozen deterministic policy; semantic claim judgement remains outside it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_source_kinds: tuple[SourceKind, ...] = Field(min_length=1, max_length=16)
    max_observation_age: timedelta = Field(gt=timedelta(0), le=timedelta(days=3650))


class EvidenceAdmissionInput(BaseModel):
    """All trusted inputs needed for one pure Evidence Admission decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: EvidenceCandidate
    artifact: ArtifactMetadata
    normalized_document: str = Field(min_length=1, max_length=2_000_000)
    active_module_ids: tuple[UUID, ...] = Field(min_length=1, max_length=128)
    observed_before: datetime
    policy: EvidenceAdmissionPolicy

    @model_validator(mode="after")
    def input_is_scoped_to_the_current_project_and_module(self) -> EvidenceAdmissionInput:
        if self.observed_before.tzinfo is None or self.observed_before.utcoffset() is None:
            raise ValueError("observed_before must be timezone-aware")
        if self.artifact.project_id != self.candidate.project_id:
            raise ValueError("artifact belongs to another project")
        if self.candidate.module_id not in self.active_module_ids:
            raise ValueError("candidate module is not active in this admission scope")
        return self


class EvidenceAdmissionDecision(BaseModel):
    """An auditable accept/reject result; only acceptance can be materialized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: UUID
    status: EvidenceAdmissionStatus
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    candidate: EvidenceCandidate


class AdmittedEvidenceBinding(BaseModel):
    """Modern accepted binding with separate relation, lifecycle and source refs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID
    project_id: UUID
    module_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    claim: str = Field(min_length=1, max_length=8_000)
    relation: EvidenceRelation
    lifecycle: EvidenceLifecycle = EvidenceLifecycle.ACTIVE
    source_span_id: UUID
    source_snapshot_id: UUID
    artifact_ref: str = Field(
        pattern=r"^artifact\+sha256://[a-f0-9]{64}/[0-9a-fA-F-]{36}$",
        max_length=512,
    )
    source_url: str = Field(min_length=1, max_length=4_000)
    span_text: str = Field(min_length=1, max_length=16_000)
    observed_at: datetime
    applicability: tuple[str, ...] = Field(default=(), max_length=64)


class EvidenceInvalidation(BaseModel):
    """A deterministic downstream trigger, not a replacement Evidence Binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_id: UUID
    candidate_id: UUID
    snapshot_id: UUID
    artifact_status: ArtifactStatus
    lifecycle: EvidenceLifecycle
    affected_coverage_keys: tuple[str, ...] = Field(max_length=128)
    targets: tuple[EvidenceInvalidationTarget, ...]

    @model_validator(mode="after")
    def coverage_keys_are_a_canonical_set(self) -> EvidenceInvalidation:
        if self.affected_coverage_keys != tuple(sorted(set(self.affected_coverage_keys))):
            raise ValueError("affected_coverage_keys must be a sorted unique tuple")
        return self


def admit_evidence_candidate(value: EvidenceAdmissionInput) -> EvidenceAdmissionDecision:
    """Apply mechanical evidence gates without an LLM or a Domain mutation."""

    candidate = value.candidate
    reason_codes: list[str] = []
    if candidate.source.kind not in value.policy.allowed_source_kinds:
        reason_codes.append("source_kind_not_allowed")
    if value.artifact.status is not ArtifactStatus.PRESENT:
        reason_codes.append("artifact_not_present")
    if value.artifact.ref != candidate.snapshot.artifact_ref:
        reason_codes.append("artifact_ref_not_metadata")
    if value.artifact.media_type != candidate.snapshot.media_type:
        reason_codes.append("artifact_media_type_mismatch")
    age = value.observed_before - candidate.observation.observed_at
    if age < timedelta(0):
        reason_codes.append("observation_after_admission_cutoff")
    elif age > value.policy.max_observation_age:
        reason_codes.append("observation_stale")
    span = candidate.span
    quoted = value.normalized_document[span.start_char : span.end_char]
    if quoted != span.quote_text:
        reason_codes.append("span_quote_not_rehydratable")
    if reason_codes:
        return EvidenceAdmissionDecision(
            candidate_id=candidate.id,
            status=EvidenceAdmissionStatus.REJECTED,
            reason_codes=tuple(reason_codes),
            candidate=candidate,
        )
    return EvidenceAdmissionDecision(
        candidate_id=candidate.id,
        status=EvidenceAdmissionStatus.ACCEPTED,
        reason_codes=("accepted",),
        candidate=candidate,
    )


def materialize_admitted_binding(
    decision: EvidenceAdmissionDecision,
    *,
    binding_id: UUID | None = None,
) -> AdmittedEvidenceBinding:
    """Map only an accepted candidate to the modern binding contract."""

    if decision.status is not EvidenceAdmissionStatus.ACCEPTED:
        raise ValueError("rejected evidence candidate cannot materialize an EvidenceBinding")
    candidate = decision.candidate
    return AdmittedEvidenceBinding(
        id=binding_id or uuid4(),
        candidate_id=candidate.id,
        project_id=candidate.project_id,
        module_id=candidate.module_id,
        basis_hash=candidate.basis_hash,
        claim=candidate.claim,
        relation=candidate.relation,
        source_span_id=candidate.span.id,
        source_snapshot_id=candidate.snapshot.id,
        artifact_ref=candidate.snapshot.artifact_ref,
        source_url=candidate.source.canonical_locator,
        span_text=candidate.span.quote_text,
        applicability=candidate.applicability,
        observed_at=candidate.observation.observed_at,
    )


def to_legacy_domain_evidence_binding(value: AdmittedEvidenceBinding) -> EvidenceBinding:
    """Temporary R2 adapter for the pre-ER-2 Domain table; it does not persist."""

    status = (
        EvidenceStatus.SUPPORTED
        if value.relation is EvidenceRelation.SUPPORTS
        else EvidenceStatus.CONTRADICTED
    )
    _, snapshot_hash = value.artifact_ref.rsplit("//", maxsplit=1)
    snapshot_hash = snapshot_hash.split("/", maxsplit=1)[0]
    return EvidenceBinding(
        id=value.id,
        project_id=value.project_id,
        module_id=value.module_id,
        claim=value.claim,
        source_url=value.source_url,
        snapshot_hash=snapshot_hash,
        span_text=value.span_text,
        status=status,
        applicability=value.applicability,
        observed_at=value.observed_at,
    )


def derive_evidence_invalidation(
    binding: AdmittedEvidenceBinding,
    *,
    artifact_status: ArtifactStatus,
    affected_coverage_keys: tuple[str, ...],
) -> EvidenceInvalidation | None:
    """Translate unusable Artifact state into one deterministic re-evaluation trigger."""

    if artifact_status is ArtifactStatus.PRESENT:
        return None
    lifecycle = (
        EvidenceLifecycle.QUARANTINED
        if artifact_status in {ArtifactStatus.CORRUPT, ArtifactStatus.QUARANTINED}
        else EvidenceLifecycle.STALE
    )
    return EvidenceInvalidation(
        project_id=binding.project_id,
        basis_hash=binding.basis_hash,
        binding_id=binding.id,
        candidate_id=binding.candidate_id,
        snapshot_id=binding.source_snapshot_id,
        artifact_status=artifact_status,
        lifecycle=lifecycle,
        affected_coverage_keys=tuple(sorted(set(affected_coverage_keys))),
        targets=(
            EvidenceInvalidationTarget.COVERAGE,
            EvidenceInvalidationTarget.VERIFICATION,
            EvidenceInvalidationTarget.IMPACT,
        ),
    )
