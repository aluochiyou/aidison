from aidison.research.evidence_ranking import (
    EvidenceCandidate,
    hamming_distance,
    rank_evidence,
    simhash,
)


def evidence(
    ref: str,
    url: str,
    title: str,
    excerpt: str = "",
    authority: float = 0.0,
) -> EvidenceCandidate:
    return EvidenceCandidate(ref, url, title, excerpt, authority)


def test_exact_url_dedup_preserves_all_source_provenance() -> None:
    ranked = rank_evidence(
        query="durable scheduler",
        candidates=(
            evidence("artifact://first", "https://example.test/scheduler", "Durable scheduler"),
            evidence("artifact://second", "https://example.test/scheduler", "Scheduler notes"),
        ),
    )

    assert len(ranked) == 1
    assert ranked[0].candidate.ref == "artifact://first"
    assert ranked[0].provenance_refs == ("artifact://first", "artifact://second")
    assert ranked[0].duplicate_urls == ("https://example.test/scheduler",)


def test_identical_text_at_different_urls_is_near_deduplicated_with_provenance() -> None:
    title = "PostgreSQL durable runtime recovery"
    ranked = rank_evidence(
        query="PostgreSQL recovery",
        candidates=(
            evidence("artifact://a", "https://a.test/runtime", title, "reclaim and fencing"),
            evidence("artifact://b", "https://b.test/mirror", title, "reclaim and fencing"),
        ),
    )

    assert simhash(f"{title} reclaim and fencing") == simhash(f"{title} reclaim and fencing")
    assert hamming_distance(simhash(title), simhash(title)) == 0
    assert len(ranked) == 1
    assert ranked[0].provenance_refs == ("artifact://a", "artifact://b")
    assert ranked[0].duplicate_urls == ("https://a.test/runtime", "https://b.test/mirror")


def test_bm25_and_rules_rank_relevant_evidence_first() -> None:
    ranked = rank_evidence(
        query="durable scheduler PostgreSQL",
        candidates=(
            evidence("artifact://unrelated", "https://example.test/z", "Pixel art workbench"),
            evidence(
                "artifact://relevant",
                "https://example.test/a",
                "PostgreSQL durable scheduler",
                "lease reclaim and transactional ready set",
                authority=0.8,
            ),
        ),
    )

    assert [item.candidate.ref for item in ranked] == [
        "artifact://relevant",
        "artifact://unrelated",
    ]
    assert ranked[0].score > ranked[1].score


def test_ties_are_stable_by_url_then_reference() -> None:
    candidates = (
        evidence("artifact://z", "https://example.test/b", "alpha"),
        evidence("artifact://a", "https://example.test/a", "beta"),
    )

    first = rank_evidence(query="no-match", candidates=candidates, near_duplicate_distance=0)
    second = rank_evidence(
        query="no-match",
        candidates=tuple(reversed(candidates)),
        near_duplicate_distance=0,
    )

    assert [item.candidate.ref for item in first] == ["artifact://a", "artifact://z"]
    assert first == second
