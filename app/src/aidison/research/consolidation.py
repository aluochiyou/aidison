"""Deterministic consolidation of already-admitted research observations.

This module never reads raw model text, runs a vote, or writes Domain state.
It turns typed observations from results that have already passed Admission into
an explainable Coverage Matrix, Conflict Set, and Sufficiency decision.
"""

from __future__ import annotations

import json
from collections import defaultdict
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.research.coverage import CoverageContract, CoveragePriority


class CoverageObservationStatus(StrEnum):
    ANSWERED = "answered"
    UNKNOWN = "unknown"


class CoverageDisposition(StrEnum):
    ANSWERED = "answered"
    UNKNOWN = "unknown"
    MISSING = "missing"
    INSUFFICIENT_SOURCES = "insufficient_sources"
    CONFLICTED = "conflicted"


class SufficiencyOutcome(StrEnum):
    WAITING = "waiting"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"
    NEEDS_VERIFICATION = "needs_verification"
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class ClaimKey(BaseModel):
    """Typed identity of a normalized claim; value is intentionally excluded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_identity: str = Field(min_length=1, max_length=500)
    predicate: str = Field(min_length=1, max_length=200)
    applicability: str = Field(min_length=1, max_length=500)
    normalization_schema: str = Field(min_length=1, max_length=120)


class ResearchCoverageObservation(BaseModel):
    """A typed observation that the caller asserts is backed by an accepted result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: UUID
    admitted_ref: str = Field(pattern=r"^admitted://.+", max_length=500)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    status: CoverageObservationStatus
    source_kinds: tuple[str, ...] = Field(max_length=16)
    # New observations carry stable source identities. Empty keeps pre-v2
    # replay fixtures readable and is treated as one distinct legacy source.
    source_ids: tuple[str, ...] = Field(default=(), max_length=16)
    # Origin is a retrieval-site boundary (for example https://docs.example),
    # not a claim about legal publisher independence. Empty legacy values fall
    # back to source_ids during consolidation.
    source_origins: tuple[str, ...] = Field(default=(), max_length=16)
    evidence_refs: tuple[str, ...] = Field(max_length=128)
    claim_key: ClaimKey | None = None
    value_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    independently_verified: bool = False

    @model_validator(mode="after")
    def answered_observation_requires_normalized_claim(self) -> ResearchCoverageObservation:
        if self.status is CoverageObservationStatus.ANSWERED:
            if self.claim_key is None or self.value_hash is None:
                raise ValueError("answered observation requires claim_key and value_hash")
            if not self.source_kinds:
                raise ValueError("answered observation requires source_kinds")
            if not self.evidence_refs:
                raise ValueError("answered observation requires evidence_refs")
        elif self.claim_key is not None or self.value_hash is not None:
            raise ValueError("unknown observation cannot carry a normalized claim value")
        return self


class ConsolidatedClaim(BaseModel):
    """One normalized value plus all admitted provenance that supports it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ClaimKey
    value_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_ids: tuple[UUID, ...]
    admitted_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]


class ResearchConflict(BaseModel):
    """Visible competing values; only an admitted independent verifier may resolve it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    key: ClaimKey
    value_hashes: tuple[str, ...] = Field(min_length=2)
    coverage_keys: tuple[str, ...]
    result_ids: tuple[UUID, ...]
    admitted_refs: tuple[str, ...]
    resolved_by_result_ids: tuple[UUID, ...] = ()


class CoverageMatrixEntry(BaseModel):
    """One Coverage Contract key's deterministic semantic projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_key: str
    priority: CoveragePriority
    status: CoverageDisposition
    result_ids: tuple[UUID, ...]
    admitted_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    observed_source_kinds: tuple[str, ...]
    observed_source_count: int = Field(default=0, ge=0)
    min_distinct_sources: int = Field(default=1, ge=1)
    observed_origin_count: int = Field(default=0, ge=0)
    min_distinct_origins: int = Field(default=1, ge=1)
    missing_source_kinds: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    requires_independent_verification: bool
    independently_verified: bool


class SufficiencyPolicy(BaseModel):
    """Execution availability, deliberately separate from Coverage Contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_expansion_available: bool = False
    verification_available: bool = False
    partial_delivery_allowed: bool = False


class SufficiencyDecision(BaseModel):
    """A product-facing route decision derived without an LLM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SufficiencyOutcome
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    gap_coverage_keys: tuple[str, ...]
    conflict_ids: tuple[str, ...]


class ConsolidationInput(BaseModel):
    """Only admitted typed observations for one frozen Coverage Contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_contract: CoverageContract
    observations: tuple[ResearchCoverageObservation, ...] = Field(max_length=512)
    pending_coverage_keys: tuple[str, ...] = Field(default=(), max_length=128)
    policy: SufficiencyPolicy = Field(default_factory=SufficiencyPolicy)

    @model_validator(mode="after")
    def observations_are_scoped_to_contract(self) -> ConsolidationInput:
        keys = {item.key for item in self.coverage_contract.keys}
        if len(set(self.pending_coverage_keys)) != len(self.pending_coverage_keys):
            raise ValueError("pending coverage keys must be unique")
        if set(self.pending_coverage_keys) - keys:
            raise ValueError("pending coverage key is absent from CoverageContract")
        for observation in self.observations:
            if observation.basis_hash != self.coverage_contract.basis_hash:
                raise ValueError("observation basis does not match CoverageContract")
            if observation.coverage_key not in keys:
                raise ValueError("observation coverage key is absent from CoverageContract")
        return self


class ConsolidationSnapshot(BaseModel):
    """Pure, deterministic output for a later Artifact and Root projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: tuple[ConsolidatedClaim, ...]
    conflicts: tuple[ResearchConflict, ...]
    coverage: tuple[CoverageMatrixEntry, ...]
    sufficiency: SufficiencyDecision


def _claim_sort_key(key: ClaimKey) -> tuple[str, str, str, str]:
    return (
        key.subject_identity,
        key.predicate,
        key.applicability,
        key.normalization_schema,
    )


def _conflict_id(key: ClaimKey, value_hashes: tuple[str, ...]) -> str:
    payload = json.dumps(
        {"key": key.model_dump(mode="json"), "value_hashes": value_hashes},
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode()).hexdigest()


def _unique_sorted(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted(values))


def _unique_sorted_ids(values: set[UUID]) -> tuple[UUID, ...]:
    return tuple(sorted(values, key=str))


def _group_claims(
    observations: tuple[ResearchCoverageObservation, ...],
) -> tuple[tuple[ConsolidatedClaim, ...], tuple[ResearchConflict, ...], dict[str, set[str]]]:
    by_claim: dict[ClaimKey, dict[str, list[ResearchCoverageObservation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for observation in observations:
        if observation.status is CoverageObservationStatus.ANSWERED:
            assert observation.claim_key is not None
            assert observation.value_hash is not None
            by_claim[observation.claim_key][observation.value_hash].append(observation)

    claims: list[ConsolidatedClaim] = []
    conflicts: list[ResearchConflict] = []
    conflict_ids_by_coverage: dict[str, set[str]] = defaultdict(set)
    for key in sorted(by_claim, key=_claim_sort_key):
        variants = by_claim[key]
        for value_hash in sorted(variants):
            group = variants[value_hash]
            claims.append(
                ConsolidatedClaim(
                    key=key,
                    value_hash=value_hash,
                    result_ids=_unique_sorted_ids({item.result_id for item in group}),
                    admitted_refs=_unique_sorted({item.admitted_ref for item in group}),
                    evidence_refs=_unique_sorted(
                        {ref for item in group for ref in item.evidence_refs}
                    ),
                )
            )
        if len(variants) > 1:
            value_hashes = tuple(sorted(variants))
            all_observations = [item for group in variants.values() for item in group]
            verifier_value_hashes = {
                value_hash
                for value_hash, group in variants.items()
                if any(item.independently_verified for item in group)
            }
            resolved_by = (
                _unique_sorted_ids(
                    {
                        item.result_id
                        for group in variants.values()
                        for item in group
                        if item.independently_verified
                    }
                )
                if len(verifier_value_hashes) == 1
                else ()
            )
            conflict_id = _conflict_id(key, value_hashes)
            coverage_keys = _unique_sorted({item.coverage_key for item in all_observations})
            if not resolved_by:
                for coverage_key in coverage_keys:
                    conflict_ids_by_coverage[coverage_key].add(conflict_id)
            conflicts.append(
                ResearchConflict(
                    id=conflict_id,
                    key=key,
                    value_hashes=value_hashes,
                    coverage_keys=coverage_keys,
                    result_ids=_unique_sorted_ids({item.result_id for item in all_observations}),
                    admitted_refs=_unique_sorted({item.admitted_ref for item in all_observations}),
                    resolved_by_result_ids=resolved_by,
                )
            )
    return tuple(claims), tuple(conflicts), conflict_ids_by_coverage


def _coverage_matrix(
    value: ConsolidationInput,
    conflict_ids_by_coverage: dict[str, set[str]],
) -> tuple[CoverageMatrixEntry, ...]:
    by_coverage: dict[str, list[ResearchCoverageObservation]] = defaultdict(list)
    for observation in value.observations:
        by_coverage[observation.coverage_key].append(observation)

    entries: list[CoverageMatrixEntry] = []
    for coverage_key in value.coverage_contract.keys:
        observations = by_coverage.get(coverage_key.key, [])
        answered = [
            item for item in observations if item.status is CoverageObservationStatus.ANSWERED
        ]
        observed_source_kinds = _unique_sorted(
            {source for item in answered for source in item.source_kinds}
        )
        missing_source_kinds = tuple(
            source
            for source in coverage_key.required_source_kinds
            if source not in observed_source_kinds
        )
        distinct_source_ids = {
            source_id
            for item in answered
            for source_id in (item.source_ids or (f"legacy:{item.result_id}",))
        }
        distinct_source_origins = {
            source_origin
            for item in answered
            for source_origin in (
                item.source_origins
                or item.source_ids
                or (f"legacy:{item.result_id}",)
            )
        }
        conflict_ids = _unique_sorted(conflict_ids_by_coverage.get(coverage_key.key, set()))
        if conflict_ids:
            status = CoverageDisposition.CONFLICTED
        elif answered and (
            missing_source_kinds
            or len(distinct_source_ids) < coverage_key.min_distinct_sources
            or len(distinct_source_origins) < coverage_key.min_distinct_origins
        ):
            status = CoverageDisposition.INSUFFICIENT_SOURCES
        elif answered:
            status = CoverageDisposition.ANSWERED
        elif observations:
            status = CoverageDisposition.UNKNOWN
        else:
            status = CoverageDisposition.MISSING
        entries.append(
            CoverageMatrixEntry(
                coverage_key=coverage_key.key,
                priority=coverage_key.priority,
                status=status,
                result_ids=_unique_sorted_ids({item.result_id for item in observations}),
                admitted_refs=_unique_sorted({item.admitted_ref for item in observations}),
                evidence_refs=_unique_sorted(
                    {ref for item in observations for ref in item.evidence_refs}
                ),
                observed_source_kinds=observed_source_kinds,
                observed_source_count=len(distinct_source_ids),
                min_distinct_sources=coverage_key.min_distinct_sources,
                observed_origin_count=len(distinct_source_origins),
                min_distinct_origins=coverage_key.min_distinct_origins,
                missing_source_kinds=missing_source_kinds,
                conflict_ids=conflict_ids,
                requires_independent_verification=coverage_key.requires_independent_verification,
                independently_verified=any(item.independently_verified for item in answered),
            )
        )
    return tuple(entries)


def _sufficiency(
    *,
    coverage: tuple[CoverageMatrixEntry, ...],
    pending_coverage_keys: tuple[str, ...],
    policy: SufficiencyPolicy,
) -> SufficiencyDecision:
    required = tuple(item for item in coverage if item.priority is CoveragePriority.MUST)
    conflicts = tuple(item for item in required if item.status is CoverageDisposition.CONFLICTED)
    if conflicts:
        conflict_ids = _unique_sorted({ref for item in conflicts for ref in item.conflict_ids})
        if policy.verification_available:
            return SufficiencyDecision(
                outcome=SufficiencyOutcome.NEEDS_VERIFICATION,
                reason_codes=("must_conflict_requires_verification",),
                gap_coverage_keys=tuple(item.coverage_key for item in conflicts),
                conflict_ids=conflict_ids,
            )
        return SufficiencyDecision(
            outcome=SufficiencyOutcome.BLOCKED,
            reason_codes=("must_conflict_without_verifier",),
            gap_coverage_keys=tuple(item.coverage_key for item in conflicts),
            conflict_ids=conflict_ids,
        )

    needs_verification = tuple(
        item
        for item in required
        if item.requires_independent_verification and not item.independently_verified
    )
    if needs_verification:
        if policy.verification_available:
            return SufficiencyDecision(
                outcome=SufficiencyOutcome.NEEDS_VERIFICATION,
                reason_codes=("must_coverage_requires_independent_verification",),
                gap_coverage_keys=tuple(item.coverage_key for item in needs_verification),
                conflict_ids=(),
            )
        return SufficiencyDecision(
            outcome=SufficiencyOutcome.BLOCKED,
            reason_codes=("independent_verification_unavailable",),
            gap_coverage_keys=tuple(item.coverage_key for item in needs_verification),
            conflict_ids=(),
        )

    pending_must = tuple(
        item.coverage_key for item in required if item.coverage_key in pending_coverage_keys
    )
    if pending_must:
        return SufficiencyDecision(
            outcome=SufficiencyOutcome.WAITING,
            reason_codes=("must_coverage_still_in_flight",),
            gap_coverage_keys=pending_must,
            conflict_ids=(),
        )

    gaps = tuple(
        item.coverage_key for item in required if item.status is not CoverageDisposition.ANSWERED
    )
    if gaps and policy.evidence_expansion_available:
        return SufficiencyDecision(
            outcome=SufficiencyOutcome.NEEDS_MORE_EVIDENCE,
            reason_codes=("must_coverage_has_bounded_gap",),
            gap_coverage_keys=gaps,
            conflict_ids=(),
        )
    if gaps and policy.partial_delivery_allowed:
        return SufficiencyDecision(
            outcome=SufficiencyOutcome.PARTIAL,
            reason_codes=("must_coverage_gap_disclosed_for_partial",),
            gap_coverage_keys=gaps,
            conflict_ids=(),
        )
    if gaps:
        return SufficiencyDecision(
            outcome=SufficiencyOutcome.BLOCKED,
            reason_codes=("must_coverage_gap_cannot_expand",),
            gap_coverage_keys=gaps,
            conflict_ids=(),
        )
    return SufficiencyDecision(
        outcome=SufficiencyOutcome.COMPLETE,
        reason_codes=("all_must_coverage_satisfied",),
        gap_coverage_keys=(),
        conflict_ids=(),
    )


def consolidate_research(value: ConsolidationInput) -> ConsolidationSnapshot:
    """Build a stable semantic projection from already-admitted observations."""

    claims, conflicts, conflict_ids_by_coverage = _group_claims(value.observations)
    coverage = _coverage_matrix(value, conflict_ids_by_coverage)
    return ConsolidationSnapshot(
        claims=claims,
        conflicts=conflicts,
        coverage=coverage,
        sufficiency=_sufficiency(
            coverage=coverage,
            pending_coverage_keys=value.pending_coverage_keys,
            policy=value.policy,
        ),
    )


__all__ = [
    "ClaimKey",
    "ConsolidatedClaim",
    "ConsolidationInput",
    "ConsolidationSnapshot",
    "CoverageDisposition",
    "CoverageMatrixEntry",
    "CoverageObservationStatus",
    "ResearchConflict",
    "ResearchCoverageObservation",
    "SufficiencyDecision",
    "SufficiencyOutcome",
    "SufficiencyPolicy",
    "consolidate_research",
]
