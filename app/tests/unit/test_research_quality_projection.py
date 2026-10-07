from uuid import uuid4

from aidison.api.app import _research_quality_payload
from aidison.research.consolidation import (
    ClaimKey,
    ConsolidationInput,
    CoverageObservationStatus,
    ResearchCoverageObservation,
    SufficiencyPolicy,
    consolidate_research,
)
from aidison.research.coverage import CoverageContract, CoverageKey, CoveragePriority


def test_quality_projection_explains_insufficient_distinct_sources() -> None:
    basis_hash = "a" * 64
    coverage = CoverageContract(
        basis_hash=basis_hash,
        objective="核对动力模块的关键约束",
        keys=(
            CoverageKey(
                key="power.acceptance.01",
                question="电机与电池能否满足推力余量？",
                priority=CoveragePriority.MUST,
                module_ids=("power-module",),
                required_source_kinds=("evidence",),
                min_distinct_sources=2,
            ),
        ),
    )
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=coverage,
            observations=(
                ResearchCoverageObservation(
                    result_id=uuid4(),
                    admitted_ref="admitted://agent-run-results/one",
                    basis_hash=basis_hash,
                    coverage_key="power.acceptance.01",
                    status=CoverageObservationStatus.ANSWERED,
                    source_kinds=("evidence",),
                    source_ids=("source:one",),
                    evidence_refs=("artifact+sha256://" + "b" * 64 + "/" + str(uuid4()),),
                    claim_key=ClaimKey(
                        subject_identity="module:power",
                        predicate="thrust_margin",
                        applicability="fixture",
                        normalization_schema="test-v1",
                    ),
                    value_hash="c" * 64,
                ),
            ),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )

    quality = _research_quality_payload(coverage=coverage, snapshot=snapshot)

    assert quality["outcome"] == "needs_more_evidence"
    assert quality["reason_codes"] == ["must_coverage_has_bounded_gap"]
    assert quality["context"] is None
    assert quality["coverage"] == [
        {
                "coverage_key": "power.acceptance.01",
                "question": "电机与电池能否满足推力余量？",
                "module_ids": ["power-module"],
            "priority": "must",
            "status": "insufficient_sources",
            "observed_source_count": 1,
            "min_distinct_sources": 2,
            "observed_origin_count": 1,
            "min_distinct_origins": 1,
                "missing_source_kinds": [],
                "observed_source_kinds": ["evidence"],
            "conflict_ids": [],
                "requires_independent_verification": False,
                "independently_verified": False,
                "rejected_claim_count": 0,
                "rejected_reason_codes": [],
                "collected_source_count": 0,
                "collection_profiles": [],
                "collected_source_kinds": [],
                "unavailable_source_reason_codes": [],
        }
    ]


def test_quality_projection_exposes_a_partial_subquery_collection_failure() -> None:
    basis_hash = "d" * 64
    coverage = CoverageContract(
        basis_hash=basis_hash,
        objective="核对两个电气约束",
        keys=(
            CoverageKey(
                key="power.current",
                question="峰值电流约束是什么？",
                priority=CoveragePriority.MUST,
                module_ids=("power-module",),
                required_source_kinds=("evidence",),
            ),
            CoverageKey(
                key="power.voltage",
                question="额定电压约束是什么？",
                priority=CoveragePriority.SHOULD,
                module_ids=("power-module",),
                required_source_kinds=("evidence",),
            ),
        ),
    )
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=coverage,
            observations=(
                ResearchCoverageObservation(
                    result_id=uuid4(),
                    admitted_ref="admitted://agent-run-results/current",
                    basis_hash=basis_hash,
                    coverage_key="power.current",
                    status=CoverageObservationStatus.ANSWERED,
                    source_kinds=("evidence",),
                    source_ids=("source:current",),
                    evidence_refs=("artifact+sha256://" + "e" * 64 + "/" + str(uuid4()),),
                    claim_key=ClaimKey(
                        subject_identity="module:power",
                        predicate="current",
                        applicability="fixture",
                        normalization_schema="test-v1",
                    ),
                    value_hash="f" * 64,
                ),
            ),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )

    quality = _research_quality_payload(
        coverage=coverage,
        snapshot=snapshot,
        source_collection_diagnostics={
            "power.voltage": {
                "collected_source_count": 1,
                "collection_profiles": ["deep"],
                "collected_source_kinds": ["evidence"],
                "unavailable_reason_codes": ["tavily_provider_unavailable"],
            }
        },
    )

    voltage = next(item for item in quality["coverage"] if item["coverage_key"] == "power.voltage")
    assert voltage["unavailable_source_reason_codes"] == ["tavily_provider_unavailable"]
    assert voltage["module_ids"] == ["power-module"]
