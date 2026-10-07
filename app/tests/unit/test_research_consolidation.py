from __future__ import annotations

from hashlib import sha256
from itertools import permutations
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.research.consolidation import (
    ClaimKey,
    ConsolidationInput,
    CoverageDisposition,
    CoverageObservationStatus,
    ResearchCoverageObservation,
    SufficiencyOutcome,
    SufficiencyPolicy,
    consolidate_research,
)
from aidison.research.coverage import CoverageContract, CoverageKey, CoveragePriority


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _contract(
    *,
    independent: bool = False,
    min_sources: int = 1,
    min_origins: int = 1,
) -> CoverageContract:
    return CoverageContract(
        basis_hash=_hash("basis"),
        objective="Choose a compatible power system.",
        keys=(
            CoverageKey(
                key="power.voltage",
                question="What voltage is compatible?",
                priority=CoveragePriority.MUST,
                module_ids=("power",),
                required_source_kinds=("specification",),
                min_distinct_sources=min_sources,
                min_distinct_origins=min_origins,
                requires_independent_verification=independent,
            ),
            CoverageKey(
                key="power.cost",
                question="What is the expected cost?",
                priority=CoveragePriority.SHOULD,
                module_ids=("power",),
                required_source_kinds=("evidence",),
            ),
        ),
    )


def _answered(
    *,
    value: str,
    result_id=None,
    source_id: str | None = None,
    source_origin: str | None = None,
    independently_verified: bool = False,
) -> ResearchCoverageObservation:
    return ResearchCoverageObservation(
        result_id=result_id or uuid4(),
        admitted_ref=f"admitted://result/{value}",
        basis_hash=_hash("basis"),
        coverage_key="power.voltage",
        status=CoverageObservationStatus.ANSWERED,
        source_kinds=("specification",),
        source_ids=(source_id or f"source:{result_id or value}",),
        source_origins=(source_origin or f"origin:{source_id or result_id or value}",),
        evidence_refs=(f"evidence://{value}",),
        claim_key=ClaimKey(
            subject_identity="battery-pack-v1",
            predicate="nominal_voltage",
            applicability="hardware-revision-a",
            normalization_schema="voltage-v1",
        ),
        value_hash=_hash(value),
        independently_verified=independently_verified,
    )


def test_same_claim_value_merges_provenance_independent_of_completion_order() -> None:
    first = _answered(value="12v", result_id=uuid4())
    second = _answered(value="12v", result_id=uuid4())
    expected = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(first, second),
            policy=SufficiencyPolicy(),
        )
    )

    for ordered in permutations((first, second)):
        snapshot = consolidate_research(
            ConsolidationInput(
                coverage_contract=_contract(),
                observations=ordered,
                policy=SufficiencyPolicy(),
            )
        )
        assert snapshot == expected

    assert len(expected.claims) == 1
    assert expected.conflicts == ()
    assert expected.coverage[0].status is CoverageDisposition.ANSWERED
    assert expected.sufficiency.outcome is SufficiencyOutcome.COMPLETE


def test_different_values_remain_visible_as_conflict_and_require_verification() -> None:
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(_answered(value="12v"), _answered(value="16v")),
            policy=SufficiencyPolicy(verification_available=True),
        )
    )

    assert len(snapshot.claims) == 2
    assert len(snapshot.conflicts) == 1
    assert snapshot.coverage[0].status is CoverageDisposition.CONFLICTED
    assert snapshot.sufficiency.outcome is SufficiencyOutcome.NEEDS_VERIFICATION
    assert snapshot.sufficiency.conflict_ids == (snapshot.conflicts[0].id,)


def test_one_independent_verifier_value_resolves_a_visible_prior_conflict() -> None:
    """A separately admitted verifier may resolve, but never erase, a conflict."""

    verifier = _answered(value="12v", independently_verified=True)
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(
                _answered(value="12v"),
                _answered(value="16v"),
                verifier,
            ),
            policy=SufficiencyPolicy(verification_available=True),
        )
    )

    assert len(snapshot.conflicts) == 1
    assert snapshot.conflicts[0].resolved_by_result_ids == (verifier.result_id,)
    assert snapshot.coverage[0].status is CoverageDisposition.ANSWERED
    assert snapshot.coverage[0].conflict_ids == ()
    assert snapshot.sufficiency.outcome is SufficiencyOutcome.COMPLETE


def test_missing_must_coverage_never_becomes_complete_from_other_successful_task() -> None:
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )

    assert snapshot.coverage[0].status is CoverageDisposition.MISSING
    assert snapshot.sufficiency.outcome is SufficiencyOutcome.NEEDS_MORE_EVIDENCE
    assert snapshot.sufficiency.gap_coverage_keys == ("power.voltage",)


def test_incomplete_source_or_independent_verification_requirement_blocks_complete() -> None:
    insufficient_source = ResearchCoverageObservation(
        result_id=uuid4(),
        admitted_ref="admitted://result/weak-source",
        basis_hash=_hash("basis"),
        coverage_key="power.voltage",
        status=CoverageObservationStatus.ANSWERED,
        source_kinds=("blog",),
        evidence_refs=("evidence://weak-source",),
        claim_key=ClaimKey(
            subject_identity="battery-pack-v1",
            predicate="nominal_voltage",
            applicability="hardware-revision-a",
            normalization_schema="voltage-v1",
        ),
        value_hash=_hash("12v"),
    )
    source_snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(insufficient_source,),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )
    assert source_snapshot.coverage[0].status is CoverageDisposition.INSUFFICIENT_SOURCES
    assert source_snapshot.sufficiency.outcome is SufficiencyOutcome.NEEDS_MORE_EVIDENCE

    verification_snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(independent=True),
            observations=(_answered(value="12v", independently_verified=False),),
            policy=SufficiencyPolicy(verification_available=True),
        )
    )
    assert verification_snapshot.coverage[0].status is CoverageDisposition.ANSWERED
    assert verification_snapshot.sufficiency.outcome is SufficiencyOutcome.NEEDS_VERIFICATION


def test_high_risk_coverage_requires_distinct_source_identities() -> None:
    same_source = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(min_sources=2),
            observations=(
                _answered(value="12v", source_id="source:motor-datasheet"),
                _answered(value="12v", source_id="source:motor-datasheet"),
            ),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )
    assert same_source.coverage[0].status is CoverageDisposition.INSUFFICIENT_SOURCES
    assert same_source.coverage[0].observed_source_count == 1
    assert same_source.sufficiency.outcome is SufficiencyOutcome.NEEDS_MORE_EVIDENCE

    two_sources = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(min_sources=2),
            observations=(
                _answered(value="12v", source_id="source:motor-datasheet"),
                _answered(value="12v", source_id="source:esc-manual"),
            ),
            policy=SufficiencyPolicy(),
        )
    )
    assert two_sources.coverage[0].status is CoverageDisposition.ANSWERED
    assert two_sources.coverage[0].observed_source_count == 2


def test_deep_coverage_rejects_two_pages_from_the_same_source_origin() -> None:
    """Different URLs on one origin are not independent cross-source evidence."""

    same_origin = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(min_sources=2, min_origins=2),
            observations=(
                _answered(
                    value="12v",
                    source_id="source:vendor-datasheet",
                    source_origin="web-origin:https://vendor.example",
                ),
                _answered(
                    value="12v",
                    source_id="source:vendor-product-page",
                    source_origin="web-origin:https://vendor.example",
                ),
            ),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )

    assert same_origin.coverage[0].status is CoverageDisposition.INSUFFICIENT_SOURCES
    assert same_origin.coverage[0].observed_source_count == 2
    assert same_origin.coverage[0].observed_origin_count == 1
    assert same_origin.sufficiency.outcome is SufficiencyOutcome.NEEDS_MORE_EVIDENCE

    different_origins = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(min_sources=2, min_origins=2),
            observations=(
                _answered(
                    value="12v",
                    source_id="source:vendor-datasheet",
                    source_origin="web-origin:https://vendor.example",
                ),
                _answered(
                    value="12v",
                    source_id="source:lab-review",
                    source_origin="web-origin:https://lab.example",
                ),
            ),
            policy=SufficiencyPolicy(),
        )
    )

    assert different_origins.coverage[0].status is CoverageDisposition.ANSWERED
    assert different_origins.coverage[0].observed_origin_count == 2


def test_partial_and_blocked_are_explicit_when_missing_must_cannot_expand() -> None:
    partial = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(),
            policy=SufficiencyPolicy(partial_delivery_allowed=True),
        )
    )
    blocked = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(),
            policy=SufficiencyPolicy(partial_delivery_allowed=False),
        )
    )

    assert partial.sufficiency.outcome is SufficiencyOutcome.PARTIAL
    assert blocked.sufficiency.outcome is SufficiencyOutcome.BLOCKED


def test_pending_must_coverage_waits_without_claiming_a_gap_or_completion() -> None:
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(),
            pending_coverage_keys=("power.voltage",),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )

    assert snapshot.sufficiency.outcome is SufficiencyOutcome.WAITING
    assert snapshot.sufficiency.reason_codes == ("must_coverage_still_in_flight",)


def test_observation_from_another_frozen_basis_is_rejected() -> None:
    foreign = _answered(value="12v").model_copy(update={"basis_hash": _hash("foreign")})

    with pytest.raises(ValidationError, match="basis"):
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(foreign,),
        )
