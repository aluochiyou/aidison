"""Pure, user-safe projection of deterministic research quality."""

from __future__ import annotations

from typing import Any

from aidison.research.coverage import CoverageContract


def build_research_quality_payload(
    *,
    coverage: CoverageContract,
    snapshot: Any,
    evidence_diagnostics: dict[str, dict[str, object]] | None = None,
    source_collection_diagnostics: dict[str, dict[str, object]] | None = None,
    context_summary: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Return the product-safe part of deterministic research consolidation."""

    contract_items = {item.key: item for item in coverage.keys}

    def diagnostic_for(coverage_key: str) -> dict[str, object]:
        value = (evidence_diagnostics or {}).get(coverage_key, {})
        return value if isinstance(value, dict) else {}

    def diagnostic_count(coverage_key: str) -> int:
        value = diagnostic_for(coverage_key).get("rejected_claim_count", 0)
        return value if isinstance(value, int) else 0

    def diagnostic_reasons(coverage_key: str) -> list[str]:
        value = diagnostic_for(coverage_key).get("rejected_reason_codes", [])
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    def collection_for(coverage_key: str) -> dict[str, object]:
        value = (source_collection_diagnostics or {}).get(coverage_key, {})
        return value if isinstance(value, dict) else {}

    def collection_count(coverage_key: str) -> int:
        value = collection_for(coverage_key).get("collected_source_count", 0)
        return value if isinstance(value, int) else 0

    def collection_strings(coverage_key: str, field: str) -> list[str]:
        value = collection_for(coverage_key).get(field, [])
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    return {
        "outcome": snapshot.sufficiency.outcome.value,
        "reason_codes": list(snapshot.sufficiency.reason_codes),
        "gap_coverage_keys": list(snapshot.sufficiency.gap_coverage_keys),
        "conflict_count": len(snapshot.conflicts),
        "context": context_summary,
        "coverage": [
            {
                "coverage_key": item.coverage_key,
                "question": contract_items[item.coverage_key].question,
                "module_ids": list(contract_items[item.coverage_key].module_ids),
                "priority": item.priority.value,
                "status": item.status.value,
                "observed_source_count": item.observed_source_count,
                "min_distinct_sources": item.min_distinct_sources,
                "observed_origin_count": item.observed_origin_count,
                "min_distinct_origins": item.min_distinct_origins,
                "missing_source_kinds": list(item.missing_source_kinds),
                "observed_source_kinds": list(item.observed_source_kinds),
                "conflict_ids": list(item.conflict_ids),
                "requires_independent_verification": item.requires_independent_verification,
                "independently_verified": item.independently_verified,
                "rejected_claim_count": diagnostic_count(item.coverage_key),
                "rejected_reason_codes": diagnostic_reasons(item.coverage_key),
                "collected_source_count": collection_count(item.coverage_key),
                "collection_profiles": collection_strings(
                    item.coverage_key, "collection_profiles"
                ),
                "collected_source_kinds": collection_strings(
                    item.coverage_key, "collected_source_kinds"
                ),
                "unavailable_source_reason_codes": collection_strings(
                    item.coverage_key, "unavailable_reason_codes"
                ),
            }
            for item in snapshot.coverage
        ],
    }


__all__ = ["build_research_quality_payload"]
